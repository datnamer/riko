"""Compile/Translate Yahoo Pipe into Python

    Takes a JSON representation of a Yahoo pipe and either:
     a) translates it into a Python script containing a function
        (using generators to build the pipeline) or
     b) compiles it as a pipeline of generators which can be executed
        in-process

    Usage:
     a) python compile.py pipe1.json
        python pipe1.py

     b) from pipe2py import compile, Context

        pipe_def = json.loads(pjson)
        pipe = parse_pipe_def(pipe_def, pipe_name)
        pipeline = build_pipeline(self.context, pipe))

        for i in pipeline:
            print i

    Instead of passing a filename, a pipe id can be passed (-p) to fetch the
    JSON from Yahoo, e.g.

        python compile.py -p 2de0e4517ed76082dcddf66f7b218057

    Author: Greg Gaughan
    Idea: Tony Hirst (http://ouseful.wordpress.com/2010/02/25/starting-to-think-about-a-yahoo-pipes-code-generator)
    Python generator pipelines inspired by:
        David Beazely (http://www.dabeaz.com/generators-uk)
    Universal Feed Parser and auto-rss modules by:
        Mark Pilgrim (http://feedparser.org)

   License: see LICENSE file
"""
import fileinput
import urllib
import sys

from functools import partial, update_wrapper
from importlib import import_module
from optparse import OptionParser
from os.path import splitext, split
from pipe2py import Context, util
from pipe2py.pprint2 import Id, repr_args, str_args
from pipe2py.topsort import topological_sort
from pipe2py.modules import pipeforever




def parse_pipe_def(pipe_def, pipe_name='anonymous'):
    """Parse pipe JSON into internal structures

    Keyword arguments:
    pipe_def -- JSON representation of the pipe
    pipe_name -- a name for the pipe (used for linking pipes)

    Returns:
    pipe -- an internal representation of a pipe
    """
    pipe = {
        'name': util.pythonise(pipe_name),
        'modules': {},
        'embed': {},
        'graph': {},
        'wires': {},
    }

    modules = pipe_def['modules']

    if not isinstance(modules, list):
        modules = [modules]

    for module in modules:
        pipe['modules'][util.pythonise(module['id'])] = module
        pipe['graph'][util.pythonise(module['id'])] = []

        if module['type'] == 'loop':
            embed = module['conf']['embed']['value']
            pipe['modules'][util.pythonise(embed['id'])] = embed
            pipe['graph'][util.pythonise(embed['id'])] = []
            pipe['embed'][util.pythonise(embed['id'])] = embed

            # make the loop dependent on its embedded module
            pipe['graph'][util.pythonise(embed['id'])].append(
                util.pythonise(module['id']))

    wires = pipe_def['wires']

    if not isinstance(wires, list):
        wires = [wires]

    for wire in wires:
        pipe['graph'][util.pythonise(wire['src']['moduleid'])].append(
            util.pythonise(wire['tgt']['moduleid']))

    # Remove any orphan nodes
    for node in pipe['graph'].keys():
        targetted = [node in pipe['graph'][k] for k in pipe['graph']]
        if not pipe['graph'][node] and not any(targetted):
            del pipe['graph'][node]

    for wire in wires:
        pipe['wires'][util.pythonise(wire['id'])] = wire

    return pipe


def build_pipeline(context, pipe):
    """Convert a pipe into an executable Python pipeline

        If context.describe_input then just return the input requirements
        instead of the pipeline

        Note: any subpipes must be available to import as .py files current
        namespace can become polluted by submodule wrapper definitions
    """
    pyinput = []
    module_sequence = topological_sort(pipe['graph'])

    # First pass to find and import any required sub-pipelines and user inputs
    # Note: assumes they have already been compiled to accessible .py files
    for module_id in module_sequence:
        module = pipe['modules'][module_id]

        if module['type'].startswith('pipe:'):
            __import__(util.pythonise(module['type']))

        if (
            module['conf']
            and 'prompt' in module['conf']
            and context.describe_input
        ):
            pyinput.append(
                (
                    module['conf']['position']['value'],
                    module['conf']['name']['value'],
                    module['conf']['prompt']['value'],
                    module['conf']['default']['type'],
                    module['conf']['default']['value']
                )
            )

            # Note: there seems to be no need to recursively collate inputs
            # from subpipelines

    if context.describe_input:
        return sorted(pyinput)

    steps = {}
    steps["forever"] = pipeforever.pipe_forever(context, None, conf=None)

    for module_id in module_sequence:
        module = pipe['modules'][module_id]
        module_type = module['type']

        # Plumb I/O

        # find the default input of this module
        input_module = steps["forever"]
        for wire in pipe['wires']:
            # if the wire is to this module and it's the default input and it's
            # the default output:
            pipe_wire = pipe['wires'][wire]

            if (
                util.pythonise(pipe_wire['tgt']['moduleid']) == module_id
                and pipe_wire['tgt']['id'] == '_INPUT'
                and pipe_wire['src']['id'].startswith('_OUTPUT')
            ):
                # todo? this equates the outputs
                input_module = steps[
                    util.pythonise(pipe_wire['src']['moduleid'])]

        if module_id in pipe['embed']:
            assert input_module == (
                steps["forever"],
                "input_module of an embedded module was already set")

            input_module = "_INPUT"

        pargs = [context, input_module]
        kargs = {"conf": module['conf']}

        # set the extra inputs of this module as kargs of this module
        for wire in pipe['wires']:
            # if the wire is to this module and it's *not* the default input
            # and it's the default output:
            pipe_wire = pipe['wires'][wire]

            if (
                util.pythonise(pipe_wire['tgt']['moduleid']) == module_id
                and pipe_wire['tgt']['id'] != '_INPUT'
                and pipe_wire['src']['id'].startswith('_OUTPUT')
            ):  # todo? this equates the outputs
                pipe_id = util.pythonise(pipe_wire['tgt']['id'])
                kargs["%(id)s" % {'id': pipe_id}] = steps[
                    util.pythonise(pipe_wire['src']['moduleid'])]

        # set the embedded module in the kargs if this is loop module
        if module_type == 'loop':
            kargs["embed"] = steps[
                util.pythonise(module['conf']['embed']['value']['id'])]

        if module_type == 'split':
            filtered = filter(
                lambda x: module_id == util.pythonise(x['src']['moduleid']),
                pipe['wires']
            )

            kargs["splits"] = len(list(filtered))

        # todo: (re)import other pipes dynamically
        pymodule_name = "pipe%s" % module_type
        pymodule_generator_name = "pipe_%s" % module_type

        if module_type.startswith('pipe:'):
            pymodule_name = "sys.modules['%s']" % util.pythonise(module_type)
            pymodule_generator_name = "%s" % util.pythonise(module_type)

        module_ref = import_module(pymodule_generator_name, pymodule_name)

        # if this module is an embedded module:
        if module_id in pipe['embed']:
            # We need to wrap submodules (used by loops) so we can pass the
            # input at runtime (as we can to sub-pipelines)
            # Note: no embed (so no subloops) or wire kargs are
            # passed and outer kwargs are passed in
            submodule = partial(
                module_ref, context, _INPUT, conf=module['conf'], **kwargs)

            # add attributes from 'module_ref' to 'submodule'
            update_wrapper(submodule, module_ref)
            submodule.__name__ = 'pipe_%s' % module_id
            steps[module_id] = submodule
        else:  # else this module is not an embedded module:
            steps[module_id] = module_ref(*pargs, **kargs)

        if context.verbose:
            print "%s (%s) = %s(%s)" % (
                steps[module_id], module_id, module_ref, str(pargs))

    return steps[module_id]


def stringify_pipe(context, pipe):
    """Convert a pipe into Python script

       If context.describe_input is passed to the script then it just
       returns the input requirements instead of the pipeline
    """

    pypipe = (
        """# Pipe %(pipename)s generated by pipe2py\n"""
        """\n"""
        """from pipe2py import Context\n"""
        """from pipe2py.modules import *\n"""
        """\n""" % {'pipename': pipe['name']}
    )

    pyinput = []

    module_sequence = topological_sort(pipe['graph'])

    # First pass to find any required subpipelines and user inputs
    for module_id in module_sequence:
        module = pipe['modules'][module_id]

        if module['type'].startswith('pipe:'):
            pypipe += """import %s\n""" % util.pythonise(module['type'])

        if module['conf'] and 'prompt' in module['conf']:
            pyinput.append(
                (
                    module['conf']['position']['value'],
                    module['conf']['name']['value'],
                    module['conf']['prompt']['value'],
                    module['conf']['default']['type'],
                    module['conf']['default']['value']
                )
            )
            # Note: there seems to be no need to recursively collate inputs
            # from subpipelines

    pypipe += (
        """\n"""
        """def %(pipename)s(context, _INPUT, conf=None, **kwargs):\n"""
        """    "Pipeline"\n"""     # todo: insert pipeline description here
        """    if conf is None:\n"""
        """        conf = {}\n"""
        """\n"""
        """    if context.describe_input:\n"""
        """        return %(inputs)s\n"""
        """\n"""
        "    forever = pipeforever.pipe_forever(context, None, conf=None)\n"
        """\n""" % {
            'pipename': pipe['name'],
            'inputs': unicode(sorted(pyinput))
        }  # todo: pprint this
    )

    prev_module = []

    for module_id in module_sequence:
        module = pipe['modules'][module_id]

        # Plumb I/O

        # find the default input of this module
        input_module = "forever"
        for wire in pipe['wires']:
            # if the wire is to this module and it's the default input and it's
            # the default output:
            pipe_wire = pipe['wires'][wire]

            if (
                util.pythonise(pipe_wire['tgt']['moduleid']) == module_id
                and pipe_wire['tgt']['id'] == '_INPUT'
                and pipe_wire['src']['id'].startswith('_OUTPUT')
            ):  # todo? this equates the outputs
                input_module = util.pythonise(pipe_wire['src']['moduleid'])

        if module_id in pipe['embed']:
            assert input_module == (
                steps["forever"],
                "input_module of an embedded module was already set")

            input_module = "_INPUT"

        mod_args = [Id('context'), Id(input_module)]
        mod_kwargs = [('conf', module['conf'])]

        # set the extra inputs of this module as kwargs of this module
        for wire in pipe['wires']:
            # if the wire is to this module and it's *not* the default input
            # and it's the default output:
            pipe_wire = pipe['wires'][wire]

            if (
                util.pythonise(pipe_wire['tgt']['moduleid']) == module_id
                and pipe_wire['tgt']['id'] != '_INPUT'
                and pipe_wire['src']['id'].startswith('_OUTPUT')
            ):  # todo? this equates the outputs
                mod_kwargs += [
                    (
                        util.pythonise(pipe_wire['tgt']['id']),
                        Id(util.pythonise(pipe_wire['src']['moduleid']))
                    )
                ]

        # set the embedded module in the kwargs if this is loop module
        if module['type'] == 'loop':
            pipe_id = util.pythonise(module['conf']['embed']['value']['id'])
            mod_kwargs += [("embed", Id("pipe_%s" % pipe_id))]

        # set splits in the kwargs if this is split module
        if module['type'] == 'split':
            filtered = filter(
                lambda x: module_id == util.pythonise(x['src']['moduleid']),
                pipe['wires']
            )

            mod_kwargs += [("splits", Id(len(list(filtered))))]

        pymodule_name = "pipe%s" % module['type']
        pymodule_generator_name = "pipe_%s" % module['type']

        if module['type'].startswith('pipe:'):
            pymodule_name = "%s" % util.pythonise(module['type'])
            pymodule_generator_name = "%s" % util.pythonise(module['type'])

        indent = ""

        if module_id in pipe['embed']:
            # We need to wrap submodules (used by loops) so we can pass the
            # input at runtime (as we can to subpipelines)
            # todo: insert submodule description here
            pypipe += (
                """    def pipe_%s(context, _INPUT, """
                """conf=None, **kwargs):\n"""
                """        "Submodule"\n""" % module_id
            )

            indent = "    "

        pypipe += (
            """%(indent)s    %(module_id)s = %(pymodule_name)s"""
            """.%(pymodule_generator_name)s(%(pargs)s)\n"""
        ) % {
            'indent': indent,
            'module_id': module_id,
            'pymodule_name': pymodule_name,
            'pymodule_generator_name': pymodule_generator_name,
            'pargs': repr_args(mod_args+mod_kwargs),
        }

        if module_id in pipe['embed']:
            pypipe += """        return %s\n""" % module_id

        prev_module = module_id

        if context.verbose:
            print (
                "%s = %s(%s)" % (
                    module_id,
                    pymodule_generator_name,
                    str_args(
                        [arg for arg in mod_args if arg != Id('context')] +
                        [
                            (key, value) for key, value in mod_kwargs
                            if key != 'conf'
                        ] +
                        [
                            (key, value) for key, value in mod_kwargs
                            if key == 'conf'
                        ]
                    )
                )
            ).encode("utf-8")

    pypipe += """    return %(module_id)s\n""" % {'module_id': prev_module}
    pypipe += (
        """\n"""
        """if __name__ == "__main__":\n"""
        """    context = Context()\n"""
        """    p = %(pipename)s(context, None)\n"""
        """    for i in p:\n"""
        """        print i\n""" % {'pipename': pipe['name']}
    )

    return pypipe






def analyze_pipe(context, pipe):
    modules = set(module['type'] for module in pipe['modules'].values())
    moduletypes = sorted(list(modules))

    if context.verbose:
        print
        print 'Modules used:', ', '.join(
            name for name in moduletypes if not name.startswith('pipe:')
        ) or None

        print 'Other pipes used:', ', '.join(
            name[5:] for name in moduletypes if name.startswith('pipe:')
        ) or None

if __name__ == '__main__':
    try:
        import json
        json.loads  # test access to the attributes of the right json module
    except (ImportError, AttributeError):
        import simplejson as json

    usage = 'usage: %prog [options] [filename]'
    parser = OptionParser(usage=usage)

    parser.add_option(
        "-p", "--pipe", dest="pipeid", help="read pipe JSON from Yahoo",
        metavar="PIPEID")
    parser.add_option(
        "-s", dest="savejson", help="save pipe JSON to file",
        action="store_true")
    parser.add_option(
        "-v", dest="verbose", help="set verbose debug", action="store_true")
    (options, args) = parser.parse_args()

    filename = args[0] if args else None
    context = Context(verbose=options.verbose)

    if options.pipeid:
        base = 'http://query.yahooapis.com/v1/public/yql?q='
        select = 'select%20PIPE.working%20from%20json%20'
        where = 'where%20url%3D%22http%3A%2F%2Fpipes.yahoo.com'
        pipe = '%2Fpipes%2Fpipe.info%3F_out%3Djson%26_id%3D'
        end = '%22&format=json'
        url = base + select + where + pipe + options.pipeid + end

        src = ''.join(urllib.urlopen(url).readlines())
        src_json = json.loads(src)
        results = src_json['query']['results']

        if not results:
            print 'Pipe not found'
            sys.exit(1)

        pjson = results['json']['PIPE']['working']
        pipe_name = 'pipe_%s' % options.pipeid
    elif filename:
        pjson = ''.join(line for line in open(filename))
        pipe_name = splitext(split(filename)[-1])[0]
    else:
        pjson = ''.join(line for line in fileinput.input())
        pipe_name = 'anonymous'

    pipe_def = json.loads(pjson)
    pipe = parse_pipe_def(pipe_def, pipe_name)

    if options.savejson:
        with open('%s.json' % pipe_name, 'w') as f:
            pprint(json.loads(pjson.encode('utf-8')), f)

    with open('%s.py' % pipe_name, 'w') as f:
        f.write(stringify_pipe(context, pipe))

    analyze_pipe(context, pipe)

    # for build example - see test/testbasics.py
