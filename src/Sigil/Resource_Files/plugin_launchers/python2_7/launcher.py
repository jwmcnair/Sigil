#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim:ts=4:sw=4:softtabstop=4:smarttab:expandtab

# Sigil Python Script Launcher
#
# This launcher script is aways invoked by the script manager 
# for python 2 scripts.  It is passed in:
# ebook_root, output directory, script type, and path to target script location
#
# This launcher script will parse the opf and make available 
# a wrapper and a type specific container object that acts as an 
# interface to be used by target scripts to safely access the ebook files.
#
# The Wrapper acts as a record keeper of changed files while acting to 
# shield the original files
#
# The Launcher script will then invoke the target's run() routine
# All target output to stdout and stderr is captured
# Upon completion simple xml formatted information is passed back 
# to Sigil before the launcher script exits

import sys
import os, os.path, urllib
import codecs

import path
from path import pathof

from opf_parser import Opf_Parser
from wrapper import Wrapper
from bookcontainer import BookContainer
from inputcontainer import InputContainer
from outputcontainer import OutputContainer

from xml.sax.saxutils import escape as xmlescape
from HTMLParser import HTMLParser
import traceback

from utf8_utils import add_cp65001_codec, utf8_argv, utf8_str
add_cp65001_codec()

_DEBUG=False

SUPPORTED_SCRIPT_TYPES = ['input', 'output', 'edit'] 

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'

EXTRA_ENTITIES = {'"':'&quot;', "'":"&apos;"}
_h = HTMLParser()

def escapeit(sval, EXTRAS=None):
    global _h
    _h = HTMLParser()
    # note, xmlescape and unescape do not work with utf-8 bytestrings
    # so pre-convert to full unicode and then convert back since our result xml is utf-8 encoded
    uval = sval.decode('utf-8')
    if EXTRAS:
        ures = xmlescape(_h.unescape(uval), EXTRAS)
    else:
        ures = xmlescape(_h.unescape(uval))
    return ures.encode('utf-8')


# Wrap a stream so that output gets saved
# using utf-8 encoding
class SavedStream:
    def __init__(self, stream, stype, ps):
        self.stream = stream
        self.encoding = stream.encoding
        self.ps = ps
        self.stype = stype
        if self.encoding == None:
            self.encoding = 'utf-8'
    def write(self, data):
        if isinstance(data,unicode):
            data = data.encode('utf-8')
        elif self.encoding not in ['utf-8','UTF-8','cp65001','CP65001']:
            udata = data.decode(self.encoding)
            data = udata.encode('utf-8')
        if self.stype == 'stdout':
            self.ps.stdouttext.append(data)
        else:
            self.ps.stderrtext.append(data)
    def __getattr__(self, attr):
        if attr == 'mode':
            return 'wb'
        if attr == 'encoding':
            return 'utf-8'
        if attr == 'stream':
            return self.stream
        return getattr(self.stream, attr)


class ProcessScript(object):
    def __init__(self, script_type, script_module, container):
        self.container = container
        self.script_module = script_module
        self.script_type = script_type
        self.exitcode = None
        self.stdouttext = []
        self.stderrtext = []
        self.wrapout = []

    def launch(self):
        script_module = self.script_module
        script_type = self.script_type
        container = self.container
        sys.stdout = SavedStream(sys.stdout, 'stdout', self)
        sys.stderr = SavedStream(sys.stderr, 'stderr', self)
        try:
            target_script = __import__(script_module)
            self.exitcode = target_script.run(container)
            sys.stdout = sys.stdout.stream
            sys.stderr = sys.stderr.stream
        except Exception, e:
            sys.stderr.write(traceback.format_exc())
            sys.stderr.write("Error: %s\n" % e)
            sys.stdout = sys.stdout.stream
            sys.stderr = sys.stderr.stream
            self.wrapout.append(_XML_HEADER)
            self.wrapout.append('<wrapper type="%s">\n<result>failed</result>\n<changes/>\n' % script_type)
            self.exitcode = -1
            return
        if script_type == "edit":
            # write out the final updated opf to the outdir
            container._w.write_opf()
        # save the wrapper results to a file before exiting the thread
        self.wrapout.append(_XML_HEADER)
        self.wrapout.append('<wrapper type="%s">\n' % script_type)
        self.wrapout.append('<result>success</result>\n')
        self.wrapout.append('<changes>\n')
        if script_type == "edit":
            for id in container._w.deleted:
                if id in container._w.id_to_href.keys():
                    href = container._w.id_to_href[id]
                    mime = container._w.id_to_mime[id]
                    bookhref = 'OEBPS/' + href
                else:
                    bookhref = id
                    id = ""
                    mime = container._w.getmime(bookhref)
                self.wrapout.append('<deleted href="%s" id="%s" media-type="%s" />\n' % (bookhref, id, mime))
        if script_type in ['input', 'edit']:
            for id in container._w.added:
                if id in container._w.id_to_href.keys():
                    href = container._w.id_to_href[id]
                    bookhref = 'OEBPS/' + href
                    mime = container._w.id_to_mime[id]
                else:
                    bookhref = id
                    id = ""
                    mime = container._w.getmime(bookhref)
                self.wrapout.append('<added href="%s" id="%s" media-type="%s" />\n' % (bookhref, id, mime))
        if script_type == 'edit':
            for id in container._w.modified.keys():
                if id in container._w.id_to_href.keys():
                    href = container._w.id_to_href[id]
                    bookhref = 'OEBPS/' + href
                    mime = container._w.id_to_mime[id]
                else:
                    bookhref = id
                    id = ""
                    mime = container._w.getmime(bookhref)
                self.wrapout.append('<modified href="%s" id="%s" media-type="%s" />\n' % (bookhref, id, mime))
        self.wrapout.append('</changes>\n')
        self.exitcode = 0
        return


def failed(script_type, msg):
    wrapper = _XML_HEADER
    if script_type is None:
        wrapper += '<wrapper>\n<result>failed</result>\n<changes/>\n'
    else:
        wrapper += '<wrapper type="%s">\n<result>failed</result>\n<changes/>\n' % script_type
    wrapper += '<msg>%s</msg>\n</wrapper>\n' % msg
    sys.stdout.write(wrapper)


# uses the utf8_arv call to properly handle full unicode paths on Windows
# arguments:
#      path to Sigil's ebook_root
#      path to Sigil's output (temp) directory
#      script type ("input", "output", "edit")
#      path to script target file

def main(argv=utf8_argv()):

    if len(argv) != 5:
        failed(None, msg="Launcher: improper number of arguments passed to launcher.py")
        return -1
    
    ebook_root = argv[1]
    outdir = argv[2]
    script_type = argv[3]
    target_file = argv[4]
    script_home = os.path.dirname(target_file)
    script_module = os.path.splitext(os.path.basename(target_file))[0]

    # do basic sanity checking anyway
    if script_type not in SUPPORTED_SCRIPT_TYPES:
        failed(None, msg="Launcher: script type %s is not supported" % script_type)
        return -1

    ok = path.exists(ebook_root) and path.isdir(ebook_root)
    ok = ok and path.exists(outdir) and path.isdir(outdir)
    ok = ok and path.exists(script_home) and path.isdir(script_home)
    ok = ok and path.exists(target_file) and path.isfile(target_file)
    if not ok:
        failed(None, msg="Launcher: missing or incorrect paths passed in")
        return -1

    # update sys with path to target module home directory
    if pathof(script_home) not in sys.path:
        sys.path.append(pathof(script_home))

    # load and parse opf if present
    op = None
    opf_path = os.path.join(ebook_root,'OEBPS','content.opf')
    if path.exists(opf_path) and path.isfile(opf_path):
        op = Opf_Parser(opf_path)

    # create a wrapper for record keeping and safety
    rk = Wrapper(ebook_root, outdir, op)

    # get the correct container
    if script_type == 'edit':
        bc = BookContainer(rk)
    elif script_type == "input":
        bc = InputContainer(rk)
    else:
        bc = OutputContainer(rk)
    
    # start the target script
    ps = ProcessScript(script_type, script_module, bc)
    ps.launch()

    # get standard error and standard out from the target script
    successmsg =  escapeit("".join(ps.stdouttext))
    errorlog =  escapeit("".join(ps.stderrtext))

    # get the target's script wrapper xml
    resultxml = "".join(ps.wrapout)
    resultxml += "<msg>\n"
    if ps.exitcode == 0:
        resultxml += successmsg
        if _DEBUG:
            resultxml += errorlog
    else:
        if _DEBUG:
            resultxml += successmsg
        resultxml += errorlog
    resultxml +='</msg>\n</wrapper>\n'

    # write it to stdout and exit
    sys.stdout.write(resultxml)
    return 0
    
if __name__ == "__main__":
    sys.exit(main())

