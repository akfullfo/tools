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
import re
import json
import urllib
import traceback
import random
import logging
import ipaddress
import crypt
import base64
import ercotsum

"""
    Simple and probably inept WSGI script to display the current
    power costs and (one day) utilization.
"""

REFRESH = 60
DELTA = 15
AGE_LIMIT = 1860

ERCOT_BASE = '/var/local/ercotsum'
ERCOT_RT = os.path.join(ERCOT_BASE, 'rt.txt')
ERCOT_DAM = os.path.join(ERCOT_BASE, 'dam.txt')

CACHE_BASE = '/var/local/www-data'
CACHE_FILE = os.path.join(CACHE_BASE, os.path.basename(ERCOT_BASE) + '.json')
CACHE_AGE = DELTA * 2

HTTP_CACHE_CONTROL = [('Cache-Control', 'no-cache, no-store, must-revalidate')]

#  Estimated kWhs used by one drier run
#
DRIER_KWH = 5

#  Backgound will be completely red when anticipated delivery is above this in cents.
#
COST_COLOR_MAX = 50

#  Demand load switch from blue to green
DEMAND_REGEN = 0.0

#  Demand load switch from green to yellosw
DEMAND_OK = 3.0

#  Demand load switch from yellow to red
DEMAND_ALARM = 7.0

log_handler = logging.StreamHandler()
log = logging.getLogger()
log_handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
log.addHandler(log_handler)
log.setLevel(logging.INFO)

LL_ENVIRON = logging.DEBUG

RE_TRUE_YES_ON = re.compile(r'^([ty]|on)', re.IGNORECASE)
RE_BASIC_AUTH = re.compile(r'^\s*Basic\s+(\S+)\s*$')

ALLOWED_NETWORK = '192.168.38.0/24'
HTPASSWD_FILE = '/var/www/mmm/etc/passwd'
IP_ALLOWED_NET = ipaddress.ip_interface(ALLOWED_NETWORK).network


def truthy(value, none=False):
    if none and value is None:
        return None
    if value is None or value == '':
        return False
    value = str(value)
    try:
        return (int(value) != 0)
    except:
        pass
    return (RE_TRUE_YES_ON.match(value) is not None)


def has_small_display(environ):
    """
        This is probably hokey by today's standards
    """
    user_agent = environ.get('HTTP_USER_AGENT')
    return (user_agent and 'android' in user_agent.lower())


def application(environ, start_response):

    def authorize():
        src_ip = environ.get('REMOTE_ADDR')
        if src_ip:
            a = ipaddress.ip_address(src_ip)
            o_net = ipaddress.ip_network((a, IP_ALLOWED_NET._prefixlen), strict=False)
            if IP_ALLOWED_NET.compare_networks(o_net) == 0:
                return True
        else:
            log.warning("No remote address available, which is weird")

        auth = environ.get('HTTP_AUTHORIZATION')
        if auth is None:
            return None

        try:
            m = RE_BASIC_AUTH.match(auth)
            if m:
                user, password = base64.b64decode(m.group(1)).decode('utf-8').split(':')
            else:
                log.error("Invalid authenication string starting %r", auth[:6])
                return False

            with open(HTPASSWD_FILE, 'rt') as f:
                for line in f:
                    if ':' in line:
                        pwuser, pwhash = line.strip().split(':')
                        if user == pwuser:
                            genhash = crypt.crypt(password, pwhash)
                            log.info("User %r password %svalidated", user, '' if pwhash == genhash else 'NOT ')
                            return pwhash == genhash
            log.warning("Unknown user %r", user)
        except Exception as e:
            log.error("Authentication failed -- %s", e)
        return False

    def money(amount):
        if amount is None:
            return None
        try:
            amount = float(amount)
        except:
            return "bad:%r" % amount

        how_big = abs(amount)
        if how_big >= 100:
            return "$%.2f" % (amount / 100.0)
        elif how_big >= 10:
            return "%.0f&#162;" % amount
        else:
            return "%.1f&#162;" % amount

    def drier_cost(cost, generation=0.0):
        if cost is None:
            return '(unknown)'
        else:
            return money(cost * (DRIER_KWH - generation))

    def constrain(val, constraint=(0.0, 1.0)):
        if val < constraint[0]:
            val = constraint[0]
        elif val > constraint[1]:
            val = constraint[1]
        return val

    def load_cache_snapshot():
        if not os.path.exists(CACHE_FILE):
            log.info("No cache file %s", CACHE_FILE)
            return None
        try:
            if os.path.getmtime(CACHE_FILE) + CACHE_AGE < time.time():
                log.debug("Cache file %s aged out", CACHE_FILE)
                return None

            with open(CACHE_FILE, 'rt') as f:
                data = json.load(f)
                log.debug("Snapshot is from %s", CACHE_FILE)
                return data
        except Exception as e:
            log.warning("Cache file %r load failed -- %s", CACHE_FILE, e)
            return None

    def save_cache_snapshot(data):
        temp = CACHE_FILE + '.tmp'
        try:
            with open(temp, 'wt') as f:
                json.dump(data, f, sort_keys=True, indent=2)
            os.rename(temp, CACHE_FILE)
            log.debug("Cache file %s update", CACHE_FILE)
        except Exception as e:
            log.warning("Cache save failed -- %s", e)

    if log.isEnabledFor(LL_ENVIRON):
        for tag, val in sorted(environ.items()):
            log.log(LL_ENVIRON, "ENV %s = %r", tag, val)

    content_type = 'text/html'

    try:
        auth = authorize()
        if not auth:
            start_response('401 Unauthorized', [
                                ('Content-Type', 'text/plain'),
                                ('WWW-Authenticate', 'Basic realm="Power"'),
                            ] + HTTP_CACHE_CONTROL)
            return ['Unauthorized'.encode('utf-8')]

        if has_small_display(environ):
            fontsz = '24px'
        else:
            fontsz = '12px'
        undercoat = 0x00
        max_shade = 0xC0
        bgcolor = '%02X%02X%02X' % (undercoat, undercoat, undercoat)

        red_color = '%02X%02X%02X' % (max_shade, 0, 0)
        yellow_color = '%02X%02X%02X' % (250, max_shade, 40)
        green_color = '%02X%02X%02X' % (0, max_shade, 0)
        blue_color = '%02X%02X%02X' % (0, 0, max_shade)

        #  Attempt to schedule the next page load on a refresh boundary.
        #
        query_args = urllib.parse.parse_qs(environ.get('QUERY_STRING', ''))
        json_webpre = ('style' in query_args and 'pre' in query_args['style'])
        json_snapshot = ('fmt' in query_args and 'json' in query_args['fmt'])
        json_dam = truthy(query_args.get('dam', [0])[0])

        now_t = time.time()
        when = REFRESH - (int(now_t) % REFRESH) + int(DELTA * random.random())
        if when <= 0:
            when = REFRESH + DELTA

        now = time.localtime(now_t)
        tim = time.strftime("%I:%M", now).lstrip('0') + time.strftime("%p", now).lower()
        dat = time.strftime("%a, %d %b", now)

        if json_dam:
            snap = None
        else:
            snap = load_cache_snapshot()

        if snap is None:
            snap = ercotsum.snapshot(dam=json_dam, log=log)
            log.debug("Snapshot is from ercotsum")
            save_cache_snapshot(snap)

        if json_snapshot:
            if json_webpre:
                resp = "<html><body><pre>\n" + json.dumps(snap, sort_keys=True, indent=2) + "\n</pre></body></html>\n"
            else:
                resp = json.dumps(snap)
                content_type = 'application/json'
        else:
            cost_cents = snap.get('next_anticipated_cents')
            demand = snap.get('demand_5m')
            demand_price = snap.get('curr_delivered_cents')
            if is not None and demand < 0.0:
                #  We only get paid the wholesale energy price when we
                #  are generating.
                #
                demand_price = snap.get('curr_spp_cents')
            if demand is None or demand_price is None:
                current_use = ''
                use_color = '%02X%02X%02X' % (0, 0, 0)
            else:
                current_use = 'Current load %.1f kW (%s/hour)' % (demand, money(demand * demand_price))
                if demand < DEMAND_REGEN:
                    use_color = blue_color
                elif demand < DEMAND_OK:
                    use_color = green_color
                elif demand < DEMAND_ALARM:
                    use_color = yellow_color
                else:
                    use_color = red_color

            generation = -demand if demand < 0.0 else 0.0

            if cost_cents:
                cost = "Current drier cost: %s per load" % drier_cost(cost_cents, generation=generation)
            else:
                cost = ''
            avg = snap.get('avg_delivered_cents', 1.0)
            as_of = snap.get('as_of', '')
            low_cost = snap.get('is_low_cost')
            peak_time = snap.get('dam_peak_next')
            peak_cents = snap.get('dam_peak_delivered')
            if low_cost:
                cheapest = "The average drier cost is %s per load" % drier_cost(avg)
            else:
                low_when = snap.get('next_low_cost')
                low_delivered = snap.get('next_low_cost_delivered')
                if low_when:
                    low_when = time.strftime("%I:%M %p", time.localtime(low_when))
                else:
                    low_when = '(unknown)'
                cheapest = "The cost should drop to %s after %s" % (drier_cost(low_delivered), low_when)

            alerts = []
            if peak_time:
                alerts.append("Peak %s/kWh at %s (%.0f%% of avg)" %
                                (money(peak_cents), time.strftime("%I:%M %p", time.localtime(peak_time)), peak_cents * 100.0 / avg))
            if snap.get('is_stale'):
                bgcolor = '606060'
                alerts.append('System problem, data is not current')
            elif cost_cents is None:
                bgcolor = 'A0A0A0'
                alerts.append('System problem, no anticipated cost')
            else:
                color_range = max_shade - undercoat
                red = green = 0.0
                if low_cost:
                    green = 1.0
                elif cost_cents >= COST_COLOR_MAX:
                    red = 1.0
                else:
                    ratio = (COST_COLOR_MAX - cost_cents) / (COST_COLOR_MAX - avg)
                    red = constrain(1.0 if ratio < 0.5 else 1.0 - (2 * (ratio - 0.5)))
                    green = constrain(1.0 if ratio > 0.5 else 2 * ratio)

                log.debug("Cost ratio: Green %.3f, RED %.3f", green, red)
                red = undercoat + int(color_range * red)
                green = undercoat + int(color_range * green)
                blue = undercoat
                bgcolor = '%02X%02X%02X' % (red, green, blue)

            style = '''
<style>
body  {background-color: #%s; font-size: %s;}
</style>''' % (bgcolor, fontsz)

            if alerts:
                alert_msg = '''
<span style="font-family:Comic Sans MS; font-size:60%; color:{red}; background-color: white">
{alerts}
</span>
'''.format(red=red_color, alerts='</br>'.join(alerts))
            else:
                alert_msg = ''

            resp = '''
<html>
<head>
<title>Bent Power</title>
<meta http-equiv="refresh" content="{refresh}"/>
{style}
</head>
<body>
<center style="font-family:helvetica; font-size:200%; color:white">
{time} {date}<br/>
<span style="font-family:Comic Sans MS; font-size:90%; color:white">
<p>{cost}</p>
</span>
<span style="font-family:Comic Sans MS; font-size:60%; color:white">
{cheapest}<br/>
(cost {delivered}, avg {average}, wholesale {wholesale} per kWh)<br/>
</span>
<pre style="font-family:Comic Sans MS; font-size:60%; background-color: white; color:{use_color}">
{current_use}
</pre>
{alerts}
</center>
</body>
</hmlt>'''.format(style=style,
                  time=tim,
                  date=dat,
                  cost=cost,
                  as_of=as_of,
                  cheapest=cheapest,
                  wholesale=money(snap.get('curr_spp_cents')),
                  delivered=money(snap.get('curr_delivered_cents')),
                  average=money(snap.get('avg_delivered_cents')),
                  current_use=current_use,
                  use_color=use_color,
                  alerts=alert_msg,
                  refresh=when)

        start_response('200 OK', [('Content-Type', content_type)] + HTTP_CACHE_CONTROL)
        return [resp.encode('utf-8')]
    except Exception as e:
        traceback.print_exc()
        code = '500 Internal error'
        start_response(code, [('Content-Type', 'text/plain')] + HTTP_CACHE_CONTROL)
        return ["%s - %s\n" % (code, e)]
