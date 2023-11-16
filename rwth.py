#!/usr/bin/env python

"""Scrape queue positions off rwth Studierendenwerk, save to db, Graph."""

import MySQLdb as sql
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import datetime as dt
import argparse
import numpy as np
import os
import sys
from collections import namedtuple
from dataclasses import dataclass
from dotenv import dotenv_values
import bisect
import re

dirname = os.path.realpath(os.path.dirname(__file__))
config = dotenv_values(dirname + "/.env")


@dataclass
class RoomRecord:
    """Class for a room record. Persisted to two tables in db."""

    room_id: int | None
    ext_room_id: int
    date: dt.date | None
    typestr: str
    description: str
    capacity: int
    pos: int


@dataclass
class User:
    """Class for a user record."""

    id: int | None
    email: str
    password: str
    rmc_value: str  # remember_me_cookie
    rmc_expiry: dt.datetime
    firstname: str
    lastname: str
    goal_date: dt.date


@dataclass
class Regression:
    """Class for a regression line."""

    m: float
    c: float
    fpos: int
    dfpos_1d: int
    dfpos_1w: int


def login_and_get_rows(db, user: User):
    """Login to RWTH site and retrieve room queue table page."""
    with requests.session() as sess:
        if user.rmc_value is None or user.rmc_expiry <= dt.datetime.now():
            # we don't have a valid "remember me cookie" so must login from scatch
            req = sess.get(config['login_url']).text
            html = BeautifulSoup(req, "html.parser")

            token = html.find("input", {"name": "_csrf_token"}).attrs["value"]

            payload = {
                "_csrf_token": token,
                "_username": user.email,
                "_password": user.password,
                "_remember_me": 'on',
            }
            action_url = urljoin(config['login_url'],
                                 html.find("form").attrs["action"])
            sess.post(action_url, data=payload)
            rmc_cookie = next(c for c in sess.cookies if c.name == 'REMEMBERME')
            user.rmc_value = rmc_cookie.value
            user.rmc_expiry = dt.datetime.fromtimestamp(rmc_cookie.expires)
            with db.cursor() as cur:
                cur.execute(
                    "update user set rmc_value = %s, rmc_expiry = %s "
                    "where id = %s",
                    (user.rmc_value, user.rmc_expiry, user.id))
                db.commit()
        else:
            # use the existing "remember me cookie" from the db
            # this is faster and more secure as it doesn't rely on pw which we shouldn't have
            optional_args = {
                'domain': config['domain'],
                'path': '/',
                'secure': True,
                'expires': user.rmc_expiry.timestamp(),
                'rest': {'HttpOnly': True}
            }
            rmc_cookie = requests.cookies.create_cookie(
                'REMEMBERME', user.rmc_value, **optional_args)
            sess.cookies.set_cookie(rmc_cookie)

        r = sess.get(config['dashboard_url'])
        soup = BeautifulSoup(r.content, "html.parser")
        return soup.find("div", id="rooms").find("table").find('tbody').find_all('tr')


def parse_row(row):
    """Parse a row of html elements and return clean namedtuple."""
    RoomRow = namedtuple('RoomRow',
                         ['typestr', 'description', 'link1', 'link2',
                          'appl_date', 'capacity', 'pos', 'del_link'])

    row = RoomRow(*row.find_all('td'))
    href = row.del_link.find("a").attrs["href"]
    ext_room_id = int(href[href.rindex("/") + 1:])

    return RoomRecord(
        None, ext_room_id, None,
        row.typestr.text.strip(), row.description.text.strip(),
        int(row.capacity.text.strip()), int(row.pos.text.strip()))


def get_db():
    """Connect to db and return an instance."""
    try:
        db = sql.connect(
            host=config['db_host'],
            user=config['db_user'],
            password=config['db_password'],
            database=config['db_database'])
        return db
    except sql.Error as e:
        print(e)
        sys.exit("Couldn't connect to db. Terminating.")


def get_or_create_room_id(db, rec: RoomRecord, user: User):
    """Retrieve room by ext_id or create a new one.

    Sets an existing or new room_id on the RoomRecord passed in.
    """
    with db.cursor() as cur:
        cur.execute("select id from room where ext_id = %s",
                    (rec.ext_room_id,))
        room_row = cur.fetchone()  # has unique index, so fetchone is safe
        if room_row is not None:
            rec.room_id = room_row[0]
        else:
            cur.execute(
                "insert into room (user_id, ext_id, type, description) \
                values (%s, %s, %s, %s)",
                (user.id, rec.ext_room_id, rec.typestr, rec.description))
            rec.room_id = cur.lastrowid

        db.commit()  # commit even the select to close the "repeatable read TX"


def create_entry(db, rec: RoomRecord):
    """Create a new entry for a room, storing new queue position."""
    with db.cursor() as cursor:
        try:
            cursor.execute(
                "insert into entry (date, room_id, capacity, pos) \
                values (%s, %s, %s, %s)",
                (rec.date, rec.room_id, rec.capacity, rec.pos)
            )
        except sql.IntegrityError as e:
            if e.args[0] == 1062:
                # warn and ignore
                print('Scraped duplicate data for ' +
                      abbrev_room(rec.typestr, rec.description) +
                      ' on ' + rec.date.strftime('%d/%m/%Y') + '. Ignoring it', file=sys.stderr)
            else:
                raise e

        db.commit()


def abbrev_room(type, description):
    """Make abbreviated description."""
    types = {
        'Wohngemeinschaft': 'WG',
        'Einzelzimmer': 'EZ',
        'Einzelapartment': 'EA'
    }
    try:
        pos_open_bracket = description.rindex('(')
        descr = description[pos_open_bracket + 1:-1]

    except ValueError:
        m = re.search(r'^([a-zäöüß]{2,5})[a-zäöüß\s]*([\d-]*)', description, re.I | re.U)
        if m is None:
            # no match, fall back to basics
            descr = description[:10]
        else:
            descr = m.group(1) + ' ' + m.group(2)

    descr = descr.ljust(11) + ' ' + types[type]
    return descr


def dates_to_ints(base: dt.date, dates: np.array):
    """Convert array of dates to array of ints = days since a base date."""
    return np.array([int((date - base).days) for date in dates])


def regress(dates, positions, max_date, goal_date, delta_days=0):
    """Regress with potential offset. dates must be sorted."""
    at = max_date - dt.timedelta(days=delta_days)
    bis = bisect.bisect_right(dates, at)
    if bis == 0:
        return None, None, None

    min_date = dates[0]
    dates_ltd = dates[:bis]
    positions_ltd = positions[:bis]
    m, c = np.polyfit(dates_to_ints(min_date, dates_ltd),
                      positions_ltd, deg=1) if len(dates_ltd) > 1 else (0, positions_ltd[0])

    # project to goal_date
    fpos = int(round(m * int((goal_date - min_date).days) + c))

    return m, c, fpos


def compute_regression(dates, positions, max_date, goal_date):
    """Compute regression line by converting to int days since min_date."""
    m, c, fpos = regress(dates, positions, max_date, goal_date)
    _, _, fpos_1d = regress(dates, positions, max_date, goal_date, delta_days=1)
    _, _, fpos_1w = regress(dates, positions, max_date, goal_date, delta_days=7)
    dfpos_1d = fpos - fpos_1d if fpos_1d is not None else None
    dfpos_1w = fpos - fpos_1w if fpos_1w is not None else None

    return Regression(m, c, fpos, dfpos_1d, dfpos_1w)


def format_delta_pos(dfpos: int | None, suffix: str):
    """Format a delta pos value with definitive `+` or `-`."""
    if dfpos is None:
        s = '?'
    elif dfpos == 0:
        s = '0'
    else:
        s = (("+" if dfpos > 0 else "-") + str(abs(dfpos)))
    s += suffix
    return s.rjust(6)


def draw_room_line(rows: np.array, user: User,
                   type: str, description: str):
    """Draw line and trend line for one one room."""
    # lazy import, because slow
    import matplotlib.pyplot as plt
    dates, positions = rows[:, 0], rows[:, 1].astype('int')
    min_date, max_date = dates[0], dates[-1]

    rl = compute_regression(dates, positions, max_date, user.goal_date)

    # plot historic data and the regression line
    label = abbrev_room(type, description) +\
        ' | ' + str(rl.fpos).rjust(3) +\
        format_delta_pos(rl.dfpos_1d, "d") +\
        format_delta_pos(rl.dfpos_1w, "w")

    line, = plt.plot(dates, positions, label=label)

    trend_dates = np.array([max_date, user.goal_date])
    trend_days = dates_to_ints(min_date, trend_dates)

    plt.plot(trend_dates, rl.m * trend_days + rl.c, line.get_color(), linestyle=':')
    return rl.fpos


def decorate_graph(user: User, legend_order, min_date, max_date, axes):
    """Factor out style and formatting steps for readability."""
    # lazy import, because slow
    import matplotlib.pyplot as plt
    import matplotlib
    import pandas
    """Decorate Graph with titles, legend and ticks."""
    # sort legend by final queue pos at goal date
    order = sorted(legend_order, key=lambda x: x.fpos, reverse=True)
    handles, labels = plt.gca().get_legend_handles_labels()
    plt.legend([handles[e.idx] for e in order], [labels[e.idx] for e in order],
               loc="upper right", prop={'family': 'monospace'}, framealpha=1)

    # calibrate axes
    plt.xlim(min_date - dt.timedelta(days=10),
             user.goal_date + dt.timedelta(days=10))
    plt.ylim(0)

    # vertical goal line, grid, titles and axes labels
    plt.axvline(x=user.goal_date, color='k', linestyle='--')
    plt.grid(visible=True, axis='y', alpha=0.3)
    plt.title('RWTH Aachen Student Accomodation', fontsize=20)
    plt.suptitle(user.firstname + " " + user.lastname +
                 " <" + user.email + ">   " + max_date.strftime('%d/%m/%Y'))
    plt.ylabel('Queue position', fontsize=15)

    # x ticks
    ticks = pandas.date_range(start=min_date, end=max_date,
                              freq='7D', inclusive='both')
    ticks = ticks.append(pandas.DatetimeIndex([user.goal_date]))
    plt.xticks(ticks, rotation=90)
    axes.xaxis.set_major_formatter(matplotlib.dates.DateFormatter('%b %d'))

    # refit everthing - deals with rotated x-labels in particular
    plt.tight_layout()


def draw_graph(db, date: dt.date, user: User, show):
    """Plot graph with projected trendlines."""
    import matplotlib.pyplot as plt
    _, ax = plt.subplots(figsize=(12, 8))
    LegendItem = namedtuple('LegendItem', ['fpos', 'idx'])
    order = []
    with db.cursor() as cur:
        cur.execute("select id, type, description from room "
                    "where user_id = %s order by id", (user.id,))
        for idx, (room_id, type, description) in enumerate(cur.fetchall()):
            cur.execute("select date, pos "
                        "from entry where room_id = %s order by date",
                        (room_id,))

            fpos = draw_room_line(np.array(cur.fetchall()), user, type, description)
            order.append(LegendItem(fpos, idx))

        cur.execute("select min(e.date) as min, max(e.date) as max "
                    "from entry e join room r on e.room_id = r.id "
                    "where r.user_id = %s",
                    (user.id,))
        overall_min_date, overall_max_date = cur.fetchone()
        db.commit()  # commit the selects to close the "repeatable read TX"

        decorate_graph(user, order, overall_min_date, overall_max_date, ax)

    if (show):
        plt.show()
    else:
        filename = dirname + '/' + overall_max_date.strftime('%Y-%m-%d') + '_' + user.email + '.png'
        plt.savefig(filename)
        print(filename)  # provide filename for calling program in shell


def scrape_queue_positions(db, date: dt.date, user: User):
    """Scrape new queue positions off site for todays date."""
    rows = login_and_get_rows(db, user)
    for row in rows:
        rec = parse_row(row)
        rec.date = date
        get_or_create_room_id(db, rec, user)
        create_entry(db, rec)


def main():
    """Parse queue positions and store in db. Or report via a graph."""
    parser = argparse.ArgumentParser(
        prog='rwth.py',
        description='Scrape date off Student accom website and store in db. '
        'Or report in graph form.',
        epilog='Hope you get a nice place to live.')

    parser.add_argument('-g', '--graph', action='store_true')
    parser.add_argument('-s', '--show', action='store_true')
    args = parser.parse_args()

    db = get_db()
    with db.cursor() as cur:
        cur.execute("select id, email, password, rmc_value, rmc_expiry,"
                    "firstname, lastname, goal_date "
                    "from user")

        user_rows = cur.fetchall()
        db.commit()  # commit the select to close the "repeatable read TX"

    date = dt.date.today()
    for row in user_rows:
        user = User(*row)
        if args.graph:
            draw_graph(db, date, user, args.show)
        else:
            scrape_queue_positions(db, date, user)


if __name__ == '__main__':
    main()
