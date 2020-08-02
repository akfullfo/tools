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

import time
import os
import json
from datetime import datetime
import dateutil.parser
from html.parser import HTMLParser
import urllib.request

#  Default location of current and historical data
DEF_BASE_DIR = '/var/local/ercotsum'

#  ERCOT load zone for North Central Texas
DEF_ZONE = 'LZ_NORTH'

#  Oncor per-kWh delivery charge for North Central Texas as of 2020-8-1
DEF_DELIVERY = 3.5448

#  Limit before real-time data is considered stale
AGE_LIMIT = 1200

DAY_SECS = 24 * 60 * 60
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

RT_FILE = 'rt.txt'
DAM_FILE = 'dam.txt'


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


def snapshot(base_dir=DEF_BASE_DIR, delivery=DEF_DELIVERY, log=None):
    def get_dam(path):
        data = {}
        if os.path.exists(path):
            with open(ercot_dam_prev, 'rt') as f:
                for line in f:
                    ts, spp_cents, delivered_cents = line.strip().split()
                    spp_cents = float(spp_cents)

                    #  This little algorithm is based on the observation that the
                    #  day-ahead-market is not a good predictor of peak price although
                    #  it might do well at predicting an hourly average.  We want
                    #  our price calculations to be more of a worst-case value, so
                    #  the algorithm tends to emphasize the peaks.
                    #
                    anticipate = spp_cents * spp_cents / 2 + delivery

                    data[ts] = (spp_cents, float(delivered_cents), anticipate)
        return data

    now_t = time.time()
    now = time.localtime(now_t)

    ercot_rt = os.path.join(base_dir, RT_FILE)
    ercot_dam = os.path.join(base_dir, DAM_FILE)
    ercot_dam_prev = os.path.join(base_dir, time.strftime("%Y%m%d", now), DAM_FILE)

    dam_data = get_dam(ercot_dam_prev)
    dam_curr = get_dam(ercot_dam)
    if dam_data != dam_curr:
        dam_data.update(dam_cur)
        if log:
            log.info("Tomorrow's DAM is in")
    else:
            log.info("Only have today's DAM")

    with open(ercot_rt, 'rt') as f:
        ts, spp_cents, delivered_cents = f.read().strip().split()

    ts = dateutil.parser.parse(ts)
    ts_t = ts.timestamp()
    age = now_t - ts_t

    snapshot = {
            "as_of": ts.strftime(DATE_FORMAT),
            "as_of_t": ts_t,
            "stale": (age > AGE_LIMIT),
            "spp_cents": float(spp_cents),
            "delivered_cents": float(delivered_cents),
            "dam": dam_data,
        }

    return snapshot
