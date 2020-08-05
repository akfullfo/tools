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
import urllib
import traceback
import logging
import ercotsum

"""
    Simple and probably inept WSGI script to display the current
    power costs and (one day) utilization.
"""

REFRESH = 60
DELTA = 10
AGE_LIMIT = 1200

ERCOT_BASE = '/var/local/ercotsum'
ERCOT_RT = os.path.join(ERCOT_BASE, 'rt.txt')
ERCOT_DAM = os.path.join(ERCOT_BASE, 'dam.txt')

#  Estimated kWhs used by one drier run
#
DRIER_KWH = 5

log_handler = logging.StreamHandler()
log = logging.getLogger()
log_handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
log.addHandler(log_handler)
log.setLevel(logging.INFO)


def application(environ, start_response):
    def drier_dollars(cost):
        return cost * DRIER_KWH / 100.0

    try:
        #  Attempt to schedule the next page load on a refresh boundary.
        #
        query_args = urllib.parse.parse_qs(environ.get('QUERY_STRING', ''))
        json_snapshot = ('fmt' in query_args and 'json' in query_args['fmt'])
        json_webpre = ('style' in query_args and 'pre' in query_args['style'])

        now_t = time.time()
        when = REFRESH - (int(now_t) % REFRESH) + DELTA
        if when <= 0:
            when = REFRESH + DELTA

        now = time.localtime(now_t)
        tim = time.strftime("%H:%M:%S", now)
        dat = time.strftime("%Y-%m-%d", now)
        snap = ercotsum.snapshot(log=log)
        if json_snapshot:
            if json_webpre:
                resp = "<pre>\n" + json.dumps(snap, sort_keys=True, indent=2) + "\n</pre>"
            else:
                resp = json.dumps(snap)
        else:
            cost = snap.get('next_anticipated_cents')
            if cost:
                cost = "Current drier cost: $%.2f per load" % drier_dollars(cost)
            else:
                cost = ''
            as_of = snap.get('as_of', '')
            low_cost = snap.get('is_low_cost')
            if low_cost:
                avg = snap.get('avg_delivered_cents', 0.0)
                cheapest = "The average drier cost is $%.2f per load" % drier_dollars(avg)
            else:
                low_when = snap.get('next_low_cost')
                low_delivered = snap.get('next_low_cost_delivered')
                if low_when:
                    low_when = time.strftime("%I:%M %p", time.localtime(low_when))
                else:
                    low_when = '(unknown)'
                cheapest = "The cost should drop to $%.2f after %s" % (drier_dollars(low_delivered), low_when)
            resp = '''
<html>
<head>
<title>Bent Power</title>
<meta http-equiv="refresh" content="{refresh}"/>
</head>
<body  bgcolor="003366">
<center style="font-family:helvetica; font-size:300%; color:white">
<h5>{date} {time}</h5>
<pre style="font-family:Comic Sans MS; font-size:70%; color:white">
{cost}
</pre>
<pre style="font-family:Comic Sans MS; font-size:50%; color:white">
{cheapest}
</pre>
</center>
</body>
</hmlt>'''.format(time=tim, date=dat, cost=cost, as_of=as_of, cheapest=cheapest, refresh=when)

        start_response('200 OK', [('Content-Type', 'text/html')])
        return [resp.encode('utf-8')]
    except Exception as e:
        traceback.print_exc()
        code = '500 Internal error'
        start_response(code, [('Content-Type', 'text/plain')])
        return ["%s - %s\n" % (code, e)]
