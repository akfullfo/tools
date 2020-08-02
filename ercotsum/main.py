#!/usr/bin/env python3
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

import os
import json
import time
import logging
import logging.handlers
import argparse

from . import DEF_BASE_DIR, DEF_DELIVERY, DEF_ZONE, DAY_SECS, DATE_FORMAT, RT_FILE, DAM_FILE, \
              PageType, Browse, fetch, snapshot

DEF_URL = 'http://www.ercot.com/content/cdr/html/real_time_spp'
DEF_PAGE_TYPE = 'RT'
DEF_LAST = 1
DEF_TIMEOUT = 30


#  These are the types of pages we can process.  The tuple elements are:
#
PAGE_TYPES = {
    'RT': PageType('http://www.ercot.com/content/cdr/html/{yyyymmdd}_real_time_spp', 1, None, RT_FILE),
    'DAM': PageType('http://www.ercot.com/content/cdr/html/{yyyymmdd}_dam_spp', 24, 14, DAM_FILE),
}

COL0_NAME = 'Oper Day'
COL1_NAMES = {
        'Interval Ending': 'RT',
        'Hour Ending': 'DAM',
}


def parse_args(argv, log=None):
    p = argparse.ArgumentParser(
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description="""
    Retrieve and parse the ERCOT real-time and day-ahead pricing from:

    When specified with a base directory, the program maintains this directory tree:

        base_dir/rt.txt                 -- The most recently read real-time data
        base_dir/dam.txt                -- The most recently read day-ahead data
        base_dir/YYYYMMDD/rt.txt        -- Accumulated real-time data for that day
        base_dir/YYYYMMDD/dam.txt       -- Day-ahead data for that day

    The typical approach for using a base directory is to have a crontab like:

        5,20,35,50 * * * *      /usr/local/bin/ercotsum -qb --real-time
        58        13 * * *      /usr/local/bin/ercotsum -qb --dam

    This covers ERCOT's real-time processing every 15 minutes which typically
    completes at 2 minutes after the quarter-hour, and ERCOTs day-ahead processing
    which typically completes at around 12:30pm every day, but they recommend accessing
    at 2pm.  The 2 minute offset there is just to avoid site congestion at the recommended
    time.

    Because the day-ahead data changes only once per day, and the data spans two days,
    the YYYYMMDD directory will be for the first date in the file which is tomorrow's
    date when run after 2pm.
    """
    )

    pt = p.add_mutually_exclusive_group(required=True)
    pt.add_argument('-d', '--dam', action='store_true', help="Fetch day-ahead market")
    pt.add_argument('-r', '--real-time', action='store_true', help="Fetch real-time prices")
    pt.add_argument('-u', '--url', action='store', metavar='addr', default=DEF_URL, help="URL to access the pricing info")
    pt.add_argument('-s', '--snapshot', action='store_true', help="Print a JSON snapshot of current data as used by web services")
    p.add_argument('-D', '--date', action='store', metavar='YYYYMMDD', help="Fetch data for specified day. Default is today")
    p.add_argument('-b', '--base-dir', nargs='?', action='store', metavar='path', const=DEF_BASE_DIR,
                    help="Base directory for recording current and historical pricing.  With no path, uses %r" % DEF_BASE_DIR)
    p.add_argument('-v', '--verbose', action='store_true', help="Verbose logging")
    p.add_argument('-q', '--quiet', action='store_true', help="Quiet logging, errors and warnings only")
    p.add_argument('-e', '--log-stderr', action='store_true', help="Log to stderr, default is syslog")
    p.add_argument('-z', '--zone', action='store', metavar='name', default=DEF_ZONE,
                    help="Zone to report pricing for, default %r" % DEF_ZONE)
    p.add_argument('-Z', '--zone-name', action='store_true', help="Include zone name in output")
    p.add_argument('-L', '--last', action='store', type=int, metavar='count',
                    help="Number of previous results to report, default %r" % DEF_LAST)
    p.add_argument('-t', '--timeout', action='store', type=int, metavar='secs', default=DEF_TIMEOUT,
                    help="Web access timeout, default %r secs" % DEF_TIMEOUT)
    p.add_argument('-f', '--file', action='store', help="Use data from this file instead of fetching the URL, used for testing")
    p.add_argument('--delivery', action='store', type=float, metavar='cents', default=DEF_DELIVERY,
                    help="Current TDU delivery change, default %.4f" % DEF_DELIVERY)

    args = p.parse_args()

    if args.log_stderr:
        log_handler = logging.StreamHandler()
        log_formatter = logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s")
    else:
        logparams = {}
        for addr in ['/dev/log', '/var/run/log']:
            if os.path.exists(addr):
                logparams['address'] = addr
                break
        log_handler = logging.handlers.SysLogHandler(**logparams)
        log_formatter = logging.Formatter(fmt="%(name)s[%(process)d]: %(levelname)s %(message).1000s")

    if not log:
        log = logging.getLogger()
        log_handler.setFormatter(log_formatter)
        log.addHandler(log_handler)

    if args.verbose:
        log.setLevel(logging.DEBUG)
    elif args.quiet:
        log.setLevel(logging.WARNING)
    else:
        log.setLevel(logging.INFO)

    page_type = PAGE_TYPES[DEF_PAGE_TYPE]
    date = time.strftime("%Y%m%d")
    if args.real_time:
        page_type = PAGE_TYPES['RT']
        if not args.date:
            args.date = date
    elif args.dam:
        page_type = PAGE_TYPES['DAM']
        if not args.date:
            now = time.time()
            lt = time.localtime(now)
            if lt.tm_hour >= page_type.cutover:
                lt = time.localtime(now + DAY_SECS)
            args.date = time.strftime("%Y%m%d", lt)
    elif not args.snapshot:
        if args.base_dir:
            raise Exception("The --base-dir flag is only supported for standard URLs")
        if not args.date:
            args.date = date
        page_type.url = args.url
        page_type.last = DEF_LAST

    args.url = page_type.url.format(yyyymmdd=args.date)

    if args.last is None:
        args.last = page_type.last

    return args, log


def output(args, page_type, text):
    if not args.base_dir:
        print(os.linesep.join(text))
        return

    hpath = os.path.join(args.base_dir, args.date)
    if not os.path.isdir(hpath):
        os.makedirs(hpath)
    hpath = os.path.join(hpath, page_type.outfile)

    new_text = os.linesep.join(text) + os.linesep
    cpath = os.path.join(args.base_dir, page_type.outfile)

    try:
        with open(cpath, 'rt') as f:
            old_text = f.read()
    except Exception as e:
        log.info("No previous data -- %s", e)
        old_text = None

    if old_text == new_text:
        log.info("No change in recorded data, skipping update")
        return

    ctemp = cpath + '.tmp'
    with open(ctemp, 'wt') as f:
        f.write(new_text)
    os.rename(ctemp, cpath)
    with open(hpath, 'at') as f:
        f.write(new_text)
    log.info("Updated %s", cpath)


def main(argv=None, ilog=None):
    global log
    args, log = parse_args(argv, log=ilog)

    if args.snapshot:
        print(json.dumps(snapshot(args.base_dir, log=log), sort_keys=True, indent=2))
        return 0

    if args.file:
        with open(args.file, 'rt') as f:
            data = f.read()
    else:
        data = fetch(args)
    b = Browse()
    b.feed(data)
    if len(b.colnames) < 3:
        log.error("Bad colname list: %s", b.colnames)
    if b.colnames[0] != COL0_NAME:
        log.error("Expecting %r as first colname, got %r", COL0_NAME, b.colnames[0])
    if b.colnames[1] not in COL1_NAMES:
        log.error("Expecting %r as second colname, got %r", ' or '.join(sorted(COL1_NAMES)), b.colnames[1])
    page_name = COL1_NAMES[b.colnames[1]]

    table_width = len(b.colnames)

    spp_table = {}
    for colpos in range(2, table_width):
        spp_table[b.colnames[colpos]] = []
    if args.zone not in spp_table:
        log.error("Selected zone %r is not in results", args.zone)
        return 2
    rownum = 0
    for row in b.rows:
        rownum += 1
        if len(row) != table_width:
            log.error("Row %d has %r columns, %d expected", rownum, len(row), table_width)
            return 2
        if page_name == 'RT':
            row_time_t = time.mktime(time.strptime(row[0] + ' ' + row[1] + '00', "%m/%d/%Y %H%M%S"))
        elif page_name == 'DAM':
            hour = int(row[1])
            inc = 0
            if hour == 24:
                inc = DAY_SECS
                hour = 0
            hour = "%02d" % hour
            row_time_t = time.mktime(time.strptime(row[0] + ' ' + hour + '0000', "%m/%d/%Y %H%M%S")) + inc
        else:
            log.error("Unknown page type %r", page_name)
            return 2
        for colpos in range(2, table_width):
            #  Convert from dollars/megawatt
            cents_per_kW = float(row[colpos]) * 100.0 / 1000.0
            spp_table[b.colnames[colpos]].append((row_time_t, cents_per_kW))

    text = []
    for pos in reversed(range(0, args.last)):
        if pos < len(spp_table[args.zone]):
            selected_recent = spp_table[args.zone][-1 - pos]
            if args.zone_name:
                text.append("%s: %s  %.2f  %.2f" % (
                            args.zone,
                            time.strftime(DATE_FORMAT, time.localtime(selected_recent[0])),
                            selected_recent[1],
                            selected_recent[1] + args.delivery))
            else:
                text.append("%s  %.2f  %.2f" % (
                            time.strftime(DATE_FORMAT, time.localtime(selected_recent[0])),
                            selected_recent[1],
                            selected_recent[1] + args.delivery))
    output(args, PAGE_TYPES[page_name], text)


if __name__ == '__main__':
    exit(main())
