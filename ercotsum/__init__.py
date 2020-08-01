# ________________________________________________________________________
#
#  Copyright (C) 2020 Andrew Fullford
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# ________________________________________________________________________
#

from html.parser import HTMLParser
import urllib.request

#  ERCOT load zone for North Central Texas
DEF_ZONE = 'LZ_NORTH'

#  Oncor per-kWh delivery charge for North Central Texas as of 2020-8-1
DEF_DELIVERY = 3.5448

DAY_SECS = 24 * 60 * 60
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class PageType(object):
    """
        Attribute class to hold standard configuration for
        supported ERCOT page types.
    """
    def __init__(self, url, last, cutover, outfile):
        self.url = url
        self.last = last
        self.cutover = cutover
        self.outfile = outfile


def fetch(args):
    resp = ''
    with urllib.request.urlopen(args.url, timeout=args.timeout) as f:
        while True:
            data = f.read(102400)
            if data:
                resp += data.decode('utf-8')
            else:
                break
    return resp


class Browse(HTMLParser):
    row = 0
    col = 0
    header = False
    colnames = []
    currow = []
    rows = []

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row += 1
        elif tag == 'td':
            self.col += 1
            self.header = False
        elif tag == 'th':
            self.col += 1
            self.header = True

    def handle_endtag(self, tag):
        if tag == 'tr':
            self.col = 0
            if self.currow and not self.header:
                self.rows.append(self.currow)
                self.currow = []

    def handle_data(self, data):
        if self.row and self.col:
            data = data.strip()
            if not data:
                return
            if self.header:
                self.colnames.append(data)
            else:
                self.currow.append(data)
