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
import datetime
import math
import dateutil.parser
from html.parser import HTMLParser
import urllib.request

#  Default location of current and historical data
DEF_BASE_DIR = '/var/local/ercotsum'

#  Default location of current and historical data
DEF_DEMAND_DIR = '/var/local/rainbarrel'

#  ERCOT load zone for North Central Texas
DEF_ZONE = 'LZ_NORTH'

#  Oncor per-kWh delivery charge for North Central Texas as of 2020-8-1
#  See https://electricityplans.com/texas/utilities/oncor/
#  Changes are Mar 1 and Sept 1 each year.
#
# DEF_DELIVERY = 3.922      # In 2020
# DEF_DELIVERY = 4.1543     # From 2021-9-1
DEF_DELIVERY = 3.5585       # From 2022-3-10

#  TDO (eg Oncor) monthly base charge in dollars/month
TDU_MONTHLY = 3.42

#  Retail (eg Griddy, Octopus) monthly service fee in dollars/month
RETAIL_MONTHY = 10.00

#  Estimated fraction for merchant services, taxes and fees
TAXES_FEES = 0.09

#  Estimated fraction for ancillary services.  This fraction seems constant for spp value.
ANCILLARY_SERVICES = 0.279

#  ERCOT per-kWh price cap as of 2020-8-1 in cents/kWh
ERCOT_PRICE_CAP = 900

#  Limit before real-time data is considered stale
AGE_LIMIT = 2000

#  Number of days to include in the real-time averaging
RT_AVG_DAYS = 35

#  Reduce the weighting in the average by this factor for
#  each day of age so that more recent costs have a
#  somewhat greater effect of the average.
#
AVG_WEIGHT = 1.05

#  Pricing is considered low-cost if it is less than
#  the RT_AVG_DAYS average multiplied by this.
#
LOW_COST_MULTIPLIER = 1.3

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


def as_delivered(price, delivery=DEF_DELIVERY):
    """
        Attempt to calculate the per-kWh rate based on the current
        price.  This caclulates a rate for the various fees that
        are related to kWh and adds a base for monthly fees applied
        to the 15 minute sample period.  That's kinda bogus but the
        intent is to estimate an instantaneous price.
    """
    extended_price = float(price) * (1.0 + ANCILLARY_SERVICES)
    delivered_price = (extended_price + delivery) * (1.0 + TAXES_FEES)
    base = (TDU_MONTHLY + RETAIL_MONTHY) / 30.0 / 24.0 / 4.0
    return float(delivered_price + base)


def snapshot(base_dir=DEF_BASE_DIR, demand_dir=DEF_DEMAND_DIR, delivery=DEF_DELIVERY, avg_days=RT_AVG_DAYS, dam=False, log=None):
    def get_dam(path):
        data = {}
        if os.path.exists(path):
            with open(path, 'rt') as f:
                for line in f:
                    ts, spp_cents = line.strip().split()[:2]
                    spp_cents = float(spp_cents)

                    #  This little algorithm is based on the observation that the
                    #  day-ahead-market is not a good predictor of peak price although
                    #  it might do well at predicting an hourly average.  We want
                    #  our price calculations to be more of a worst-case value, so
                    #  the algorithm tends to emphasize the peaks.
                    #
                    anticipate = spp_cents * spp_cents / 2
                    if anticipate > ERCOT_PRICE_CAP:
                        anticipate = ERCOT_PRICE_CAP
                    if anticipate < spp_cents:
                        anticipate = spp_cents

                    data[ts] = (spp_cents, as_delivered(spp_cents, delivery=delivery), as_delivered(anticipate, delivery=delivery))
        return data

    def get_rt_average(base_dir, as_of, days):
        """
            Find the average real-time pricing for the last N days.  This returns a
            weighted average which reduces each day's weight by AVG_WEIGHT so that the
            most recent days have a much stringer effect than the subsequent days.

            It also returns a raw average.
        """
        total = 0.0
        count = 0
        weighted_total = 0.0
        weight = 0.0
        weighting = 1.0

        for day in range(days):
            when = time.localtime(as_of - day * DAY_SECS)
            path = os.path.join(base_dir, time.strftime("%Y%m%d", when), RT_FILE)
            if os.path.exists(path):
                with open(path, 'rt') as f:
                    for line in f:
                        ts, spp_cents = line.strip().split()[:2]
                        val = as_delivered(spp_cents, delivery=delivery)
                        total += val
                        count += 1
                        weighted_total += val * weighting
                        weight += weighting
            weighting /= AVG_WEIGHT
        if count > 0:
            return round(weighted_total / weight, 4), round(total / count, 4)
        else:
            if log:
                log.info("Average pricing not available")
            return None, None, None

    def demand_avg(demand, limit):
        count = 0
        total = 0.0
        for ts, kw in demand:
            if ts >= limit:
                count += 1
                total += kw
            else:
                break
        if count > 0:
            avg = round(total / count, 4)
        else:
            avg = None
        if log:
            log.debug("Found %d items, total %.1f, avg %.1f", count, total, avg)
        return avg

    def get_demand_load(base_dir, as_of):
        """
            Find the average demand load in kW.  The result is a
            tuple (1m, 5m, 15m) a la Unix loadavg.

            This data comes via rainbarrel monitoring of the
            Rainforest Eagle zigbee gateway.

            This loads data for the last two days so that at the
            midnight transition there will be at least one day
            of data available.
        """

        #  Only require this many lines to do the averging.
        #  This avoids doing timestamp calculations on large
        #  numbers of samples.  The date parsing is very slow.
        #
        need_lines = int(900 / 30 + 100)

        demand_lines = []
        for day in range(1, -1, -1):
            when = time.localtime(as_of - day * DAY_SECS)
            path = os.path.join(base_dir, time.strftime("%Y-%m-%d.demand", when))
            if os.path.exists(path):
                with open(path, 'rt') as f:
                    for line in f:
                        demand_lines.append(line.strip())

        demand = []
        for line in demand_lines[-need_lines:]:
            try:
                iso_time, demand_kw = line.split()
                ts_t = dateutil.parser.parse(iso_time).timestamp()
                demand.append((ts_t, float(demand_kw)))
            except Exception as e:
                if log:
                    log.warning("Ingnoring demand entry %r -- %s", line, e)

        if demand:
            demand.sort(reverse=True)
            if demand[0][0] < as_of - AGE_LIMIT:
                if log:
                    log.warning("Stale demand data detected, most recent is %.1f hours old", as_of - demand[0][0])

            return (demand_avg(demand, as_of - 60), demand_avg(demand, as_of - 300), demand_avg(demand, as_of - 900))
        else:
            return (None, None, None)

    now_t = time.time()
    now = datetime.datetime.now(tz=dateutil.tz.tzlocal())

    ercot_rt = os.path.join(base_dir, RT_FILE)
    ercot_dam = os.path.join(base_dir, DAM_FILE)
    ercot_dam_prev = os.path.join(base_dir, now.strftime("%Y%m%d"), DAM_FILE)

    dam_data = get_dam(ercot_dam_prev)
    dam_curr = get_dam(ercot_dam)
    if dam_data != dam_curr:
        dam_data.update(dam_curr)
        if log:
            log.debug("Tomorrow's DAM is in")
    else:
        if log:
            log.debug("Only have today's DAM")

    #  The time stamp text of the most recent hour.
    #
    now_hr = now.replace(minute=0, second=0, microsecond=0).strftime(DATE_FORMAT)

    rt_avg, rt_raw_avg = get_rt_average(base_dir, now_t, avg_days)
    low_cost_level = rt_avg * LOW_COST_MULTIPLIER

    demand = get_demand_load(demand_dir, now_t)

    dam_current = None
    dam_next_below = None
    dam_peak_next = None
    dam_peak = 0.0
    peak_limit = time.strftime(DATE_FORMAT, time.localtime(now_t + 22 * 3600))

    for dam_date in sorted(dam_data):
        if dam_date >= now_hr:
            if not dam_current:
                dam_current = dam_data[dam_date]
            if dam_date <= peak_limit and dam_peak < dam_data[dam_date][1] and dam_data[dam_date][1] > low_cost_level:
                dam_peak = dam_data[dam_date][1]
                dam_peak_next = dam_date
            if not dam_next_below and dam_current and dam_date > now_hr and dam_data[dam_date][1] < low_cost_level:
                dam_next_below = dam_date

    if dam_next_below == now_hr:
        low_cost = True
        dam_next_below = None
    else:
        low_cost = False

    with open(ercot_rt, 'rt') as f:
        ts, spp_cents = f.read().strip().split()[:2]

    ts = dateutil.parser.parse(ts)
    ts_t = ts.timestamp()
    mtime_t = os.path.getmtime(ercot_rt)
    age = now_t - ts_t
    if log:
        log.info("Loaded %r: ts %.0f, mtime %.0f, age %.0f, limit %.0f", ercot_rt, ts_t, mtime_t, age, AGE_LIMIT)

    delivered_cents = as_delivered(spp_cents, delivery=delivery)

    #  Cost level is an integer where 0 means low cost, > 0 means
    #  successively higher cost.  Any value < 0 means we get paid
    #  to waste power, which is weird but apparently happens every
    #  now any then.
    #
    #  Note that this is based on the real-time rate and not on the
    #  anticipated rate.  Operations that can't react quickly such
    #  as a clothes-drier run should use the "is_low_cost" flag.
    #
    cost_level = math.floor(delivered_cents / low_cost_level)

    snapshot = {
            "as_of": ts.strftime(DATE_FORMAT),
            "as_of_t": ts_t,
            "is_stale": (age > AGE_LIMIT),
            "curr_spp_cents": float(spp_cents),
            "curr_delivered_cents": delivered_cents,
            "avg_delivered_cents": rt_avg,
            "raw_avg_delivered_cents": rt_raw_avg,
            "demand_1m": demand[0],
            "demand_5m": demand[1],
            "demand_15m": demand[2],
            "cost_level": cost_level,
        }
    if dam_current:
        anticipated = dam_current[2]
        if anticipated < delivered_cents:
            anticipated = delivered_cents
        low_cost = (anticipated < low_cost_level)
        snapshot["next_spp_cents"] = dam_current[0]
        snapshot["next_delivered_cents"] = dam_current[1]
        snapshot["next_anticipated_cents"] = anticipated
    if not low_cost and dam_next_below:
        snapshot["next_low_cost"] = dateutil.parser.parse(dam_next_below).timestamp()
        snapshot["next_low_cost_delivered"] = dam_data[dam_next_below][1]
    if dam_peak_next:
        snapshot["dam_peak_next"] = dateutil.parser.parse(dam_peak_next).timestamp()
        snapshot["dam_peak_delivered"] = dam_data[dam_peak_next][1]

    if dam:
        snapshot["dam"] = dam_data

    snapshot["is_low_cost"] = low_cost

    return snapshot
