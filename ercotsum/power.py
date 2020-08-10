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
import random
import logging
import ercotsum

"""
    Simple and probably inept WSGI script to display the current
    power costs and (one day) utilization.
"""

REFRESH = 60
DELTA = 15
AGE_LIMIT = 1200

ERCOT_BASE = '/var/local/ercotsum'
ERCOT_RT = os.path.join(ERCOT_BASE, 'rt.txt')
ERCOT_DAM = os.path.join(ERCOT_BASE, 'dam.txt')

#  Estimated kWhs used by one drier run
#
DRIER_KWH = 5

#  Backgound will be completely red when anticipated delivery is above this in cents.
#
COST_COLOR_MAX = 50

log_handler = logging.StreamHandler()
log = logging.getLogger()
log_handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
log.addHandler(log_handler)
log.setLevel(logging.INFO)

LL_ENVIRON = logging.DEBUG


def has_small_display(environ):
    """
        This is probably hokey by today's standards
    """
    user_agent = environ.get('HTTP_USER_AGENT')
    return (user_agent and 'android' in user_agent.lower())


def application(environ, start_response):
    def drier_dollars(cost):
        return cost * DRIER_KWH / 100.0

    def constrain(val, constraint=(0.0, 1.0)):
        if val < constraint[0]:
            val = constraint[0]
        elif val > constraint[1]:
            val = constraint[1]
        return val

    if log.isEnabledFor(LL_ENVIRON):
        for tag, val in environ.items():
            log.log(LL_ENVIRON, "ENV %s = %r", tag, val)

    if has_small_display(environ):
        fontsz = '24px'
    else:
        fontsz = '12px'
    undercoat = 0x00
    max_shade = 0xC0
    bgcolor = '%02X%02X%02X' % (undercoat, undercoat, undercoat)

    try:
        #  Attempt to schedule the next page load on a refresh boundary.
        #
        query_args = urllib.parse.parse_qs(environ.get('QUERY_STRING', ''))
        json_snapshot = ('fmt' in query_args and 'json' in query_args['fmt'])
        json_webpre = ('style' in query_args and 'pre' in query_args['style'])

        now_t = time.time()
        when = REFRESH - (int(now_t) % REFRESH) + int(DELTA * random.random())
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
            cost_cents = snap.get('next_anticipated_cents')
            demand_5m = snap.get('demand_5m')
            demand_price = snap.get('curr_delivered_cents')
            if demand_5m < 0.0:
                #  We only get paid the wholesale energy price when we
                #  are generating.
                #
                demand_price = snap.get('curr_spp_cents')
            if demand_5m is None or demand_price is None:
                current_use = ''
            else:
                current_use = 'Current load %.1f kW ($%.2f/hour)' % (demand_5m, demand_5m * demand_price / 100.0)

            if cost_cents:
                cost = "Current drier cost: $%.2f per load" % drier_dollars(cost_cents)
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

            if snap.get('is_stale'):
                bgcolor = '606060'
                stale = 'System problem, data is not current'
            else:
                stale = ''
                color_range = max_shade - undercoat
                red = green = 0
                if low_cost:
                    green = 1.0
                elif cost_cents >= COST_COLOR_MAX:
                    red = 1.0
                else:
                    ratio = (COST_COLOR_MAX - cost_cents) / (COST_COLOR_MAX - avg)
                    red = constrain(1.0 if ratio < 0.5 else 1.0 - (2 * (ratio - 0.5)))
                    green = constrain(1.0 if ratio > 0.5 else 2 * ratio)

                red = undercoat + int(color_range * red)
                green = undercoat + int(color_range * green)
                blue = undercoat
                bgcolor = '%02X%02X%02X' % (red, green, blue)

            style = '''
<style>
body  {background-color: #%s; font-size: %s;}
</style>''' % (bgcolor, fontsz)

            resp = '''
<html>
<head>
<title>Bent Power</title>
<meta http-equiv="refresh" content="{refresh}"/>
{style}
</head>
<body>
<center style="font-family:helvetica; font-size:300%; color:white">
<h5>{date} {time}</h5>
<pre style="font-family:Comic Sans MS; font-size:70%; color:white">
{cost}
</pre>
<pre style="font-family:Comic Sans MS; font-size:40%; color:white">
{cheapest}
(wholesale {wholesale:.2f} c/kWh, delivered {delivered:.2f} c/kWh)
{current_use}
</pre>
<pre style="font-family:Comic Sans MS; font-size:70%; color:red">
{stale}
</pre>
</center>
</body>
</hmlt>'''.format(style=style,
                  time=tim,
                  date=dat,
                  cost=cost,
                  as_of=as_of,
                  cheapest=cheapest,
                  wholesale=snap.get('curr_spp_cents'),
                  delivered=snap.get('curr_delivered_cents'),
                  current_use=current_use,
                  stale=stale,
                  refresh=when)

        start_response('200 OK', [('Content-Type', 'text/html')])
        return [resp.encode('utf-8')]
    except Exception as e:
        traceback.print_exc()
        code = '500 Internal error'
        start_response(code, [('Content-Type', 'text/plain')])
        return ["%s - %s\n" % (code, e)]
