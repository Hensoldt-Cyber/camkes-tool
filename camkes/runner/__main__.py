#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright 2017, Data61
# Commonwealth Scientific and Industrial Research Organisation (CSIRO)
# ABN 41 687 119 230.
#
# This software may be distributed and modified according to the terms of
# the BSD 2-Clause license. Note that NO WARRANTY is provided.
# See "LICENSE_BSD2.txt" for details.
#
# @TAG(DATA61_BSD)
#

'''Entry point for the runner (template instantiator).'''
from __future__ import absolute_import, division, print_function, \
    unicode_literals

import \
    six, string, sys, traceback, pickle
import re
import os
import numbers
import locale
import jinja2
import functools
import argparse
from capdl import ObjectType, ObjectAllocator, CSpaceAllocator, \
    lookup_architecture, AddressSpaceAllocator, AllocatorState
from camkes.runner.Renderer import Renderer
from camkes.internal.exception import CAmkESError
import camkes.internal.log as log
from camkes.templates import Templates, PLATFORMS, TemplateError
from camkes.ast import ASTError, Connection, Connector
from simpleeval import simple_eval

import yaml

from camkes.internal.seven import zip

# Excuse this horrible prelude. When running under a different interpreter we
# have an import path that doesn't include dependencies like elftools and
# Jinja. We need to juggle the import path prior to importing them. Note, this
# code has no effect when running under the standard Python interpreter.
import platform
import subprocess
import sys

from capdl.Object import register_object_sizes

if platform.python_implementation() != 'CPython':
    path = eval(subprocess.check_output(['python', '-c',
                                         'import sys; sys.stdout.write(\'%s\' % sys.path)'],
                                        universal_newlines=True))
    for p in path:
        if p not in sys.path:
            sys.path.append(p)


def _die(options, message):

    if isinstance(message, (list, tuple)):
        for line in message:
            log.error(line)
    else:
        log.error(message)

    tb = traceback.format_exc()
    log.debug('\n --- Python traceback ---\n%s ------------------------\n' % tb)
    sys.exit(-1)


def parse_args(argv, out, err):
    parser = argparse.ArgumentParser(prog='python -m camkes.runner',
                                     description='instantiate templates based on a CAmkES specification')
    parser.add_argument('--quiet', '-q', help='No diagnostics.', dest='verbosity',
                        default=1, action='store_const', const=0)
    parser.add_argument('--verbose', '-v', help='Verbose diagnostics.',
                        dest='verbosity', action='store_const', const=2)
    parser.add_argument('--debug', '-D', help='Extra verbose diagnostics.',
                        dest='verbosity', action='store_const', const=3)
    parser.add_argument('--outfile', '-O', help='Output to the given file.',
                        type=argparse.FileType('w'), required=True, action='append', default=[])
    parser.add_argument('--verification-base-name', type=str,
                        help='Prefix to use when generating Isabelle theory files.')
    parser.add_argument('--item', '-T', help='AST entity to produce code for.',
                        required=True, action='append', default=[])
    parser.add_argument('--platform', '-p', help='Platform to produce code '
                        'for. Pass \'help\' to see valid platforms.', default='seL4',
                        choices=PLATFORMS)
    parser.add_argument('--templates', '-t', help='Extra directories to '
                        'search for templates (before builtin templates).', action='append',
                        default=[])
    parser.add_argument('--frpc-lock-elision', action='store_true',
                        default=True, help='Enable lock elision optimisation in seL4RPC '
                        'connector.')
    parser.add_argument('--fno-rpc-lock-elision', action='store_false',
                        dest='frpc_lock_elision', help='Disable lock elision optimisation in '
                        'seL4RPC connector.')
    parser.add_argument('--fspecialise-syscall-stubs', action='store_true',
                        default=True, help='Generate inline syscall stubs to reduce overhead '
                        'where possible.')
    parser.add_argument('--fno-specialise-syscall-stubs', action='store_false',
                        dest='fspecialise_syscall_stubs', help='Always use the libsel4 syscall '
                        'stubs.')
    parser.add_argument('--fprovide-tcb-caps', action='store_true',
                        default=True, help='Hand out TCB caps to components, allowing them to '
                        'exit cleanly.')
    parser.add_argument('--fno-provide-tcb-caps', action='store_false',
                        dest='fprovide_tcb_caps', help='Do not hand out TCB caps, causing '
                        'components to fault on exiting.')
    parser.add_argument('--default-priority', type=int, default=254,
                        help='Default component thread priority.')
    parser.add_argument('--default-max-priority', type=int, default=254,
                        help='Default component thread maximum priority.')
    parser.add_argument('--default-affinity', type=int, default=0,
                        help='Default component thread affinity.')
    parser.add_argument('--default-period', type=int, default=10000,
                        help='Default component thread scheduling context period.')
    parser.add_argument('--default-budget', type=int, default=10000,
                        help='Default component thread scheduling context budget.')
    parser.add_argument('--default-data', type=int, default=0,
                        help='Default component thread scheduling context data.')
    parser.add_argument('--default-size_bits', type=int, default=8,
                        help='Default scheduling context size bits.')
    parser.add_argument('--default-stack-size', type=int, default=16384,
                        help='Default stack size of each thread.')
    parser.add_argument('--largeframe', action='store_true',
                        help='Use large frames (for non-DMA pools) when possible.')
    parser.add_argument('--architecture', '--arch', default='aarch32',
                        type=lambda x: type('')(x).lower(),
                        choices=('aarch32', 'arm_hyp', 'ia32', 'x86_64',
                                 'aarch64', 'riscv32', 'riscv64'),
                        help='Target architecture.')
    parser.add_argument('--makefile-dependencies', '-MD',
                        type=argparse.FileType('w'), help='Write Makefile dependency rule to '
                        'FILE')
    parser.add_argument('--debug-fault-handlers', action='store_true',
                        help='Provide fault handlers to decode cap and VM faults for '
                        'debugging purposes.')
    parser.add_argument('--largeframe-dma', action='store_true',
                        help='Use large frames for DMA pools when possible.')
    parser.add_argument('--realtime', action='store_true',
                        help='Target realtime seL4.')
    parser.add_argument('--object-sizes', type=argparse.FileType('r'),
                        help='YAML file specifying the object sizes for any seL4 objects '
                        'used in this invocation of the runner.')

    object_state_group = parser.add_mutually_exclusive_group()
    object_state_group.add_argument('--load-object-state', type=argparse.FileType('rb'),
                                    help='Load previously-generated cap and object state.')
    object_state_group.add_argument('--save-object-state', type=argparse.FileType('wb'),
                                    help='Save generated cap and object state to this file.')

    # To get the AST, there should be either a pickled AST or a file to parse
    parser.add_argument('--load-ast', type=argparse.FileType('rb'),
                        help='Load specification AST from this file.', required=True)

    # Juggle the standard streams either side of parsing command-line arguments
    # because argparse provides no mechanism to control this.
    old_out = sys.stdout
    old_err = sys.stderr
    sys.stdout = out
    sys.stderr = err
    options = parser.parse_args(argv[1:])

    sys.stdout = old_out
    sys.stderr = old_err

    # Check that verification_base_name would be a valid identifer before
    # our templates try to use it
    if options.verification_base_name is not None:
        if not re.match(r'[a-zA-Z][a-zA-Z0-9_]*$', options.verification_base_name):
            parser.error('Not a valid identifer for --verification-base-name: %r' %
                         options.verification_base_name)

    return options


def rendering_error(item, exn):
    """Helper to format an error message for template rendering errors."""
    tb = '\n'.join(traceback.format_tb(sys.exc_info()[2]))
    return (['While rendering %s: %s' % (item, line) for line in exn.args] +
            ''.join(tb).splitlines())


def main(argv, out, err):

    # We need a UTF-8 locale, so bail out if we don't have one. More
    # specifically, things like the version() computation traverse the file
    # system and, if they hit a UTF-8 filename, they try to decode it into your
    # preferred encoding and trigger an exception.
    encoding = locale.getpreferredencoding().lower()
    if encoding not in ('utf-8', 'utf8'):
        err.write('CAmkES uses UTF-8 encoding, but your locale\'s preferred '
                  'encoding is %s. You can override your locale with the LANG '
                  'environment variable.\n' % encoding)
        return -1

    options = parse_args(argv, out, err)

    # register object sizes with loader
    if options.object_sizes:
        register_object_sizes(yaml.load(options.object_sizes, Loader=yaml.FullLoader))

    # Ensure we were supplied equal items and outfiles
    if len(options.outfile) != len(options.item):
        err.write('Different number of items and outfiles. Required one outfile location '
                  'per item requested.\n')
        return -1

    # No duplicates in items or outfiles
    if len(set(options.item)) != len(options.item):
        err.write('Duplicate items requested through --item.\n')
        return -1
    if len(set(options.outfile)) != len(options.outfile):
        err.write('Duplicate outfiles requrested through --outfile.\n')
        return -1

    # Save us having to pass debugging everywhere.
    die = functools.partial(_die, options)

    log.set_verbosity(options.verbosity)

    ast = pickle.load(options.load_ast)

    # Locate the assembly.
    assembly = ast.assembly
    if assembly is None:
        die('No assembly found')

    # Do some extra checks if the user asked for verbose output.
    if options.verbosity >= 2:

        # Try to catch type mismatches in attribute settings. Note that it is
        # not possible to conclusively evaluate type correctness because the
        # attributes' type system is (deliberately) too loose. That is, the
        # type of an attribute can be an uninterpreted C type the user will
        # provide post hoc.
        for i in assembly.composition.instances:
            for a in i.type.attributes:
                value = assembly.configuration[i.name].get(a.name)
                if value is not None:
                    if a.type == 'string' and not \
                            isinstance(value, six.string_types):
                        log.warning('attribute %s.%s has type string but is '
                                    'set to a value that is not a string' % (i.name,
                                                                             a.name))
                    elif a.type == 'int' and not \
                            isinstance(value, numbers.Number):
                        log.warning('attribute %s.%s has type int but is set '
                                    'to a value that is not an integer' % (i.name,
                                                                           a.name))
    templates = Templates(options.platform)
    [templates.add_root(t) for t in options.templates]
    try:
        r = Renderer(templates)
    except jinja2.exceptions.TemplateSyntaxError as e:
        die('template syntax error: %s' % e)

    # The user may have provided their own connector definitions (with
    # associated) templates, in which case they won't be in the built-in lookup
    # dictionary. Let's add them now. Note, definitions here that conflict with
    # existing lookup entries will overwrite the existing entries. Note that
    # the extra check that the connector has some templates is just an
    # optimisation; the templates module handles connectors without templates
    # just fine.
    for c in (x for x in ast.items if isinstance(x, Connector) and
              (x.from_template is not None or x.to_template is not None)):
        try:
            # Find a connection that uses this type.
            connection = next(x for x in ast if isinstance(x, Connection) and
                              x.type == c)
            # Add the custom templates and update our collection of read
            # inputs.
            templates.add(c, connection)
        except TemplateError as e:
            die('while adding connector %s: %s' % (c.name, e))
        except StopIteration:
            # No connections use this type. There's no point adding it to the
            # template lookup dictionary.
            pass

    if options.load_object_state is not None:
        render_state = pickle.load(options.load_object_state)

    else:
        obj_space = ObjectAllocator()
        obj_space.spec.arch = options.architecture
        render_state = AllocatorState(obj_space=obj_space)

        for i in assembly.composition.instances:
            # Don't generate any code for hardware components.
            if i.type.hardware:
                continue

            key = i.address_space

            if key not in render_state.cspaces:
                cnode = render_state.obj_space.alloc(ObjectType.seL4_CapTableObject,
                                                     name="%s_cnode" % key, label=key)
                render_state.cspaces[key] = CSpaceAllocator(cnode)
                pd = obj_space.alloc(lookup_architecture(options.architecture).vspace().object, name="%s_group_bin_pd" % key,
                                     label=key)
                addr_space = AddressSpaceAllocator(
                    re.sub(r'[^A-Za-z0-9]', '_', "%s_group_bin" % key), pd)
                render_state.pds[key] = pd
                render_state.addr_spaces[key] = addr_space

    for (item, outfile) in zip(options.item, options.outfile):
        key = item.split("/")
        if len(key) is 1:
            # We are rendering something that isn't a component or connection.
            i = assembly
            obj_key = None
            template = templates.lookup(item)
        elif key[1] in ["source", "header", "c_environment_source", "cakeml_start_source", "cakeml_end_source", "camkesConstants", "linker", "debug", "simple", "rump_config"]:
            # We are rendering a component template
            i = [x for x in assembly.composition.instances if x.name == key[0]][0]
            obj_key = i.address_space
            template = templates.lookup(item, i)
        elif key[1] in ["from", "to"]:
            # We are rendering a connection template
            c = [c for c in assembly.composition.connections if c.name == key[0]][0]
            if key[1] == "to":
                i = c.to_ends[int(key[-1])]
            elif key[1] == "from":
                i = c.from_ends[int(key[-1])]
            else:
                die("Invalid connector end")
            obj_key = i.instance.address_space
            template = templates.lookup("/".join(key[:-1]), c)
        else:
            die("item: \"%s\" does not have the correct formatting to lookup a template." % item)
        try:
            g = r.render(i, assembly, template, render_state, obj_key,
                         outfile_name=outfile.name, options=options, my_pd=render_state.pds[obj_key] if obj_key else None)
            outfile.write(g)
            outfile.close()
        except TemplateError as inst:
            die(rendering_error(i.name, inst))

    read = r.get_files_used()
    # Write a Makefile dependency rule if requested.
    if options.makefile_dependencies is not None:
        options.makefile_dependencies.write('%s: \\\n  %s\n' %
                                            (options.outfile[0].name, ' \\\n  '.join(sorted(read))))

    if options.save_object_state is not None:
        # Write the render_state to the supplied outfile
        pickle.dump(render_state, options.save_object_state)

    sys.exit(0)


if __name__ == '__main__':
    sys.exit(main(sys.argv, sys.stdout, sys.stderr))
