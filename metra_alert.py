#!/usr/bin/env python3
"""
Metra train watcher.

Every weekday morning this watches Metra's official live feed for one train
(default: BNSF #1236). The moment the train leaves Aurora, it texts you the
EXPECTED (live) arrival time at your station (default: Lisle). If the train is
running more than 5 minutes late, it adds the reason Metra has posted.

Usage:
  python metra_alert.py              # normal run (used by the daily schedule)
  python metra_alert.py --dry-run    # print what's happening right now, no text sent
  python metra_alert.py --test-sms   # send a test text to make sure texting works
"""
import argparse
import csv
import io
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import smtplib
from email.message import EmailMessage

import requests
from google.transit import gtfs_realtime_pb2 as rt

# ---------------------------------------------------------------- settings
TZ = ZoneInfo("America/Chicago")
TRAIN = os.getenv("TRAIN_NUMBER", "1236")
WATCH_STOP = os.getenv("WATCH_STOP", "Aurora")   # text is sent when the train leaves here
MY_STOP = os.getenv("MY_STOP", "Lisle")          # the station you board at
DELAY_THRESHOLD_MIN = int(os.getenv("DELAY_THRESHOLD_MIN", "5"))
POLL_SECONDS = 30                                # Metra updates every 30 s
NO_DATA_GRACE_MIN = 10                           # after this, assume "on schedule"

STATIC_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
RT_BASE = "https://gtfspublic.metrarr.com/gtfs/public"
TRAIN_PAT = re.compile(rf"(^|[^0-9]){re.escape(TRAIN)}([^0-9]|$)")


def log(*a):
    print(datetime.now(TZ).strftime("[%H:%M:%S]"), *a, flush=True)


def fmt(dt):
    return dt.strftime("%-I:%M %p")


# ---------------------------------------------------------------- schedule (static GTFS)
def load_static():
    r = requests.get(STATIC_URL, timeout=90)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())

    def read(name):
        if name not in names:
            return []
        with z.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")))

    return {n: read(n) for n in
            ["trips.txt", "stops.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt"]}


def active_services(g, day):
    ymd, dow = day.strftime("%Y%m%d"), day.strftime("%A").lower()
    active = {c["service_id"] for c in g["calendar.txt"]
              if c["start_date"] <= ymd <= c["end_date"] and c.get(dow) == "1"}
    for cd in g["calendar_dates.txt"]:
        if cd["date"] == ymd:
            if cd["exception_type"] == "1":
                active.add(cd["service_id"])
            else:
                active.discard(cd["service_id"])
    return active


def find_trip(g, day):
    services = active_services(g, day)
    for t in g["trips.txt"]:
        if t["service_id"] not in services or "BNSF" not in t["route_id"].upper():
            continue
        if TRAIN_PAT.search(t["trip_id"]) or TRAIN_PAT.search(t.get("trip_short_name", "")):
            return t
    return None


def trip_stops(g, trip_id):
    names = {s["stop_id"]: s["stop_name"] for s in g["stops.txt"]}
    sts = sorted((st for st in g["stop_times.txt"] if st["trip_id"] == trip_id),
                 key=lambda s: int(s["stop_sequence"]))

    def find(label):
        label = label.lower()
        for st in sts:
            if label == st["stop_id"].lower() or label in names.get(st["stop_id"], "").lower():
                return st
        return None

    return sts, find(WATCH_STOP), find(MY_STOP)


def gtfs_time(day, hhmmss):
    h, m, s = map(int, hhmmss.split(":"))
    return datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(hours=h, minutes=m, seconds=s)


# ---------------------------------------------------------------- live data (GTFS-realtime)
def fetch(feed):
    key = os.environ["METRA_API_KEY"]
    r = requests.get(f"{RT_BASE}/{feed}", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    msg = rt.FeedMessage()
    msg.ParseFromString(r.content)
    return msg


def is_my_trip(td, trip_id):
    if td.trip_id == trip_id:
        return True
    return bool(TRAIN_PAT.search(td.trip_id)) and "BNSF" in (td.route_id or td.trip_id).upper()


def live_status(trip_id):
    tu = next((e.trip_update for e in fetch("tripupdates").entity
               if e.HasField("trip_update") and is_my_trip(e.trip_update.trip, trip_id)), None)
    vp = next((e.vehicle for e in fetch("positions").entity
               if e.HasField("vehicle") and is_my_trip(e.vehicle.trip, trip_id)), None)
    return tu, vp


def update_seq(u, seq_of):
    return u.stop_sequence or seq_of.get(u.stop_id, 0)


def has_left(stop, sts, tu, vp, now):
    """True once the train has departed `stop`."""
    seq_of = {st["stop_id"]: int(st["stop_sequence"]) for st in sts}
    wseq = int(stop["stop_sequence"])

    # 1) GPS position is the most direct signal
    if vp is not None:
        cur = vp.current_stop_sequence if vp.HasField("current_stop_sequence") else seq_of.get(vp.stop_id)
        if cur:
            return cur > wseq

    # 2) Fall back to the trip-update predictions
    if tu is not None and tu.stop_time_update:
        seqs = [update_seq(u, seq_of) for u in tu.stop_time_update]
        if all(s > wseq for s in seqs if s):  # passed stops get dropped from the feed
            return True
        for u in tu.stop_time_update:
            if update_seq(u, seq_of) == wseq:
                ev = u.departure if u.HasField("departure") else u.arrival
                if ev.time and now.timestamp() > ev.time + 30:
                    return True
    return False


def expected_arrival(stop, sts, tu, day):
    """(scheduled, expected) arrival at `stop`."""
    sched = gtfs_time(day, stop["arrival_time"])
    if tu is None:
        return sched, sched
    seq_of = {st["stop_id"]: int(st["stop_sequence"]) for st in sts}
    by_seq = {int(st["stop_sequence"]): st for st in sts}
    my_seq = int(stop["stop_sequence"])
    last_delay = None
    for u in sorted(tu.stop_time_update, key=lambda u: update_seq(u, seq_of)):
        seq = update_seq(u, seq_of)
        ev = u.arrival if u.HasField("arrival") else u.departure
        if seq == my_seq:
            if u.schedule_relationship == rt.TripUpdate.StopTimeUpdate.SKIPPED:
                return sched, None
            if ev.time:
                return sched, datetime.fromtimestamp(ev.time, TZ)
            if ev.HasField("delay"):
                return sched, sched + timedelta(seconds=ev.delay)
        if seq and seq < my_seq:
            if ev.HasField("delay"):
                last_delay = ev.delay
            elif ev.time and seq in by_seq:
                ref = by_seq[seq]
                last_delay = ev.time - gtfs_time(day, ref["arrival_time"]).timestamp()
    if last_delay is not None:  # carry the most recent known delay forward
        return sched, sched + timedelta(seconds=last_delay)
    if tu.HasField("delay"):
        return sched, sched + timedelta(seconds=tu.delay)
    return sched, sched


def delay_reason(trip_id):
    """Best-matching Metra service alert: train-specific first, then BNSF line-wide."""
    try:
        feed = fetch("alerts")
    except Exception as e:
        log("Could not load alerts:", e)
        return None
    best = None
    for e in feed.entity:
        if not e.HasField("alert"):
            continue
        a = e.alert
        header = " ".join(t.text for t in a.header_text.translation[:1])
        desc = " ".join(t.text for t in a.description_text.translation[:1])
        text = re.sub(r"<[^>]+>", " ", f"{header}. {desc}")
        text = re.sub(r"\s+", " ", text).strip(" .")
        on_trip = any(ie.HasField("trip") and is_my_trip(ie.trip, trip_id) for ie in a.informed_entity)
        on_line = any("BNSF" in ie.route_id.upper() for ie in a.informed_entity)
        score = 2 if (on_trip or TRAIN_PAT.search(text)) else 1 if on_line else 0
        if score == 0:
            continue
        cause = rt.Alert.Cause.Name(a.cause) if a.HasField("cause") else "UNKNOWN_CAUSE"
        if cause not in ("UNKNOWN_CAUSE", "OTHER_CAUSE"):
            text = f"{text} [{cause.replace('_', ' ').lower()}]"
        if best is None or score > best[0]:
            best = (score, text)
    if best is None:
        return None
    score, text = best
    text = text if len(text) <= 280 else text[:277] + "..."
    return text if score == 2 else f"(line-wide alert, may be related) {text}"


# ---------------------------------------------------------------- texting
def send_sms(body, dry=False):
    """Send the alert. Default: email through your own Gmail (free)."""
    log("ALERT:\n" + body)
    if dry:
        return
    provider = os.getenv("SMS_PROVIDER", "email").lower()
    if provider == "email":
        sender = os.environ["GMAIL_ADDRESS"]
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = os.getenv("EMAIL_TO") or sender
        msg["Subject"] = body.splitlines()[0] if "\n" not in body else body.splitlines()[1]
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
            smtp.send_message(msg)
    elif provider == "twilio":
        sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                          data={"To": os.environ["PHONE_NUMBER"], "From": os.environ["TWILIO_FROM"],
                                "Body": body}, auth=(sid, token), timeout=20)
        r.raise_for_status()
    else:  # textbelt
        r = requests.post("https://textbelt.com/text",
                          data={"phone": os.environ["PHONE_NUMBER"], "message": body,
                                "key": os.getenv("TEXTBELT_KEY") or "textbelt"}, timeout=20)
        res = r.json()
        if not res.get("success"):
            raise RuntimeError(f"Textbelt error: {res}")
    log("Alert sent.")


def build_message(trip_id, sts, my_stop, tu, day):
    sched, exp = expected_arrival(my_stop, sts, tu, day)
    if exp is None:
        msg = f"⚠️ BNSF #{TRAIN} left {WATCH_STOP} but Metra shows it SKIPPING {MY_STOP} today."
        reason = delay_reason(trip_id)
        return msg + (f"\nReason: {reason}" if reason else "")
    late = round((exp - sched).total_seconds() / 60)
    if late > 0:
        status = f"{late} min late"
    elif late < 0:
        status = f"{-late} min early"
    else:
        status = "on time"
    msg = (f"🚆 BNSF #{TRAIN} just left {WATCH_STOP}.\n"
           f"Expected at {MY_STOP}: {fmt(exp)} (scheduled {fmt(sched)}, {status}).")
    if late > DELAY_THRESHOLD_MIN:
        reason = delay_reason(trip_id)
        msg += f"\nLikely reason: {reason}" if reason else "\nMetra hasn't posted a reason yet."
    return msg


# ---------------------------------------------------------------- main
def correct_dst_slot(now):
    """The workflow fires at two UTC times so it runs at ~6:05 AM Chicago time
    year-round; only the one matching today's daylight-saving offset proceeds."""
    cron = os.getenv("TRIGGER_CRON", "").strip()
    if not cron:
        return True  # started by hand
    hour = cron.split()[1]
    offset = now.utcoffset().total_seconds() / 3600
    return (hour == "11" and offset == -5) or (hour == "12" and offset == -6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-sms", action="store_true")
    args = ap.parse_args()

    if args.test_sms:
        send_sms(f"✅ Test from your Metra #{TRAIN} watcher. Texts are working!")
        return

    now = datetime.now(TZ)
    today = now.date()
    if not args.dry_run:
        if not correct_dst_slot(now):
            log("Other daylight-saving slot handles today. Exiting.")
            return
        if today.weekday() >= 5:
            log("Weekend. Exiting.")
            return

    log("Loading Metra schedule...")
    g = load_static()
    trip = find_trip(g, today)
    if trip is None:
        log(f"Train #{TRAIN} is not scheduled today (holiday or schedule change). Exiting.")
        return
    trip_id = trip["trip_id"]
    sts, watch, mine = trip_stops(g, trip_id)
    if not watch or not mine:
        sys.exit(f"Couldn't find '{WATCH_STOP}' or '{MY_STOP}' on trip {trip_id}.")

    sched_watch = gtfs_time(today, watch["departure_time"])
    sched_mine = gtfs_time(today, mine["arrival_time"])
    log(f"Trip {trip_id}: scheduled {WATCH_STOP} {fmt(sched_watch)}, {MY_STOP} {fmt(sched_mine)}")

    if args.dry_run:
        tu, vp = live_status(trip_id)
        log("Live trip update found:", tu is not None, "| GPS position found:", vp is not None)
        if tu is not None or vp is not None:
            log(f"Has left {WATCH_STOP}:", has_left(watch, sts, tu, vp, datetime.now(TZ)))
        send_sms(build_message(trip_id, sts, mine, tu, today), dry=True)
        return

    start = sched_watch - timedelta(minutes=15)
    wait = (start - datetime.now(TZ)).total_seconds()
    if wait > 0:
        log(f"Sleeping until {fmt(start)}...")
        time.sleep(wait)

    deadline = sched_mine + timedelta(minutes=60)
    no_data_deadline = sched_watch + timedelta(minutes=NO_DATA_GRACE_MIN)
    while datetime.now(TZ) < deadline:
        now = datetime.now(TZ)
        try:
            tu, vp = live_status(trip_id)
        except Exception as e:
            log("Feed error, retrying:", e)
            time.sleep(POLL_SECONDS)
            continue

        if tu is not None and tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
            reason = delay_reason(trip_id)
            send_sms(f"❌ Metra shows BNSF #{TRAIN} CANCELED today."
                     + (f"\nReason: {reason}" if reason else ""))
            return

        if has_left(watch, sts, tu, vp, now):
            send_sms(build_message(trip_id, sts, mine, tu, today))
            return

        if tu is None and vp is None and now > no_data_deadline:
            send_sms(f"ℹ️ No live tracking for BNSF #{TRAIN} right now. Metra treats that as "
                     f"on schedule: {MY_STOP} at {fmt(sched_mine)}.")
            return

        time.sleep(POLL_SECONDS)

    log("Train never reported leaving the watch stop. No text sent.")


if __name__ == "__main__":
    main()
