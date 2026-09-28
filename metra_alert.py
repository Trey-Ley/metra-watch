#!/usr/bin/env python3
"""
Metra BNSF watcher + delay diary.

Every weekday morning:
  * ALERT TRAIN (#1236): emails you when it leaves Aurora with the live Lisle
    arrival time, then again at Route 59 / Naperville ONLY if it's running late.
  * DIARY TRAINS (#1236, #1308, #1310): records the actual delay at Aurora,
    Route 59, Naperville, Lisle and Union Station, plus Metra's stated reason,
    as one row per train in your Google Sheet.

Usage:
  python metra_alert.py               # normal run (used by the daily schedule)
  python metra_alert.py --dry-run     # email a snapshot of right now (no diary row)
  python metra_alert.py --test-sms    # send a test email
  python metra_alert.py --test-sheet  # write a test row to a "Test" tab in your sheet
"""
import argparse
import csv
import io
import json
import os
import re
import smtplib
import sys
import time
import zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2 as rt

# ---------------------------------------------------------------- settings
TZ = ZoneInfo("America/Chicago")
ALERT_TRAIN = os.getenv("TRAIN_NUMBER", "1236")
DIARY_TRAINS = [t.strip() for t in os.getenv("DIARY_TRAINS", "1236,1308,1310").split(",") if t.strip()]
WATCH_STOP = os.getenv("WATCH_STOP", "Aurora")   # alert email when the train leaves here
MY_STOP = os.getenv("MY_STOP", "Lisle")          # your station
# Stops between Aurora and Lisle where you get an update email ONLY if the train is late
CHECKPOINTS = [c.strip() for c in os.getenv("CHECKPOINTS", "Route 59,Naperville").split(",") if c.strip()]
# Stops recorded in the diary ("Union Station" = the train's final stop)
DIARY_STOPS = ["Aurora", "Route 59", "Naperville", "Lisle", "Union Station"]
DELAY_THRESHOLD_MIN = int(os.getenv("DELAY_THRESHOLD_MIN", "5"))
POLL_SECONDS = 30
NO_DATA_GRACE_MIN = 10
LATE_FALLBACK_MIN = int(os.getenv("LATE_FALLBACK_MIN", "8"))  # assume departed this long after schedule

STATIC_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
RT_BASE = "https://gtfspublic.metrarr.com/gtfs/public"

HEADERS = (["Date", "Day", "Train", "Sched Aurora", "Sched Lisle"]
           + [f"{s} delay (min)" for s in DIARY_STOPS]
           + ["Max delay (min)", "Status", "Metra's reason", "Data source"])

STOPPED_AT = rt.VehiclePosition.STOPPED_AT


def log(*a):
    print(datetime.now(TZ).strftime("[%H:%M:%S]"), *a, flush=True)


def fmt(dt):
    return dt.strftime("%-I:%M %p") if dt else ""


def train_pat(num):
    return re.compile(rf"(^|[^0-9]){re.escape(num)}([^0-9]|$)")


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
            rows = csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"), skipinitialspace=True)
            # Metra's files have stray spaces/odd casing in headers and values; normalize them.
            return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in rows]

    return {n: read(n) for n in
            ["trips.txt", "stops.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt"]}


def active_services(g, day):
    ymd, dow = day.strftime("%Y%m%d"), day.strftime("%A").lower()
    active = {c.get("service_id") for c in g["calendar.txt"]
              if c.get("start_date", "0") <= ymd <= c.get("end_date", "99999999") and c.get(dow) == "1"}
    for cd in g["calendar_dates.txt"]:
        if cd.get("date") == ymd:
            if cd.get("exception_type") == "1":
                active.add(cd["service_id"])
            else:
                active.discard(cd["service_id"])
    return active


def find_trip(g, day, num):
    pat = train_pat(num)
    services = active_services(g, day)
    candidates = [t for t in g["trips.txt"]
                  if "BNSF" in t.get("route_id", "").upper()
                  and (pat.search(t.get("trip_id", "")) or pat.search(t.get("trip_short_name", "")))]
    for t in candidates:
        if t.get("service_id") in services:
            return t
    if candidates and not services:
        log(f"Warning: couldn't read Metra's service calendar; matching #{num} on train number only.")
        return candidates[0]
    if not candidates:
        log(f"No BNSF trip numbered {num} in the schedule. Sample BNSF trip IDs:",
            [t.get("trip_id") for t in g["trips.txt"] if "BNSF" in t.get("route_id", "").upper()][:5])
    return None


def gtfs_time(day, hhmmss):
    h, m, s = map(int, hhmmss.split(":"))
    return datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(hours=h, minutes=m, seconds=s)


# ---------------------------------------------------------------- live data (GTFS-realtime)
def fetch(feed):
    r = requests.get(f"{RT_BASE}/{feed}",
                     headers={"Authorization": f"Bearer {os.environ['METRA_API_KEY']}"}, timeout=20)
    r.raise_for_status()
    msg = rt.FeedMessage()
    msg.ParseFromString(r.content)
    return msg


def snapshot():
    """One download of the live feeds, shared by all trains."""
    tus = [e.trip_update for e in fetch("tripupdates").entity if e.HasField("trip_update")]
    vps = [e.vehicle for e in fetch("positions").entity if e.HasField("vehicle")]
    return tus, vps


def is_trip(td, trip_id, pat):
    if td.trip_id == trip_id:
        return True
    return bool(pat.search(td.trip_id)) and "BNSF" in (td.route_id or td.trip_id).upper()


def delay_reason(trip_id, pat):
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
        on_trip = any(ie.HasField("trip") and is_trip(ie.trip, trip_id, pat) for ie in a.informed_entity)
        on_line = any("BNSF" in ie.route_id.upper() for ie in a.informed_entity)
        score = 2 if (on_trip or pat.search(text)) else 1 if on_line else 0
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


def status_text(sched, exp):
    late = round((exp - sched).total_seconds() / 60)
    if late > 0:
        return late, f"{late} min late"
    if late < 0:
        return late, f"{-late} min early"
    return late, "on time"


# ---------------------------------------------------------------- one train
class Train:
    def __init__(self, g, day, num, trip, alert=False):
        self.num, self.day, self.alert = num, day, alert
        self.pat = train_pat(num)
        self.trip_id = trip["trip_id"]
        names = {s["stop_id"]: s["stop_name"] for s in g["stops.txt"]}
        self.sts = sorted((st for st in g["stop_times.txt"] if st["trip_id"] == self.trip_id),
                          key=lambda s: int(s["stop_sequence"]))
        self.seq_of = {st["stop_id"]: int(st["stop_sequence"]) for st in self.sts}
        self.by_seq = {int(st["stop_sequence"]): st for st in self.sts}

        def find(label):
            for lab in (label.lower(), label.lower().replace("route", "rt")):
                for st in self.sts:
                    if lab == st["stop_id"].lower() or lab in names.get(st["stop_id"], "").lower():
                        return st
            return None

        self.find = find
        self.first, self.final = self.sts[0], self.sts[-1]
        self.watch, self.mine = find(WATCH_STOP), find(MY_STOP)
        self.diary = {}  # label -> stop_time (or None if this train doesn't stop there)
        for label in DIARY_STOPS:
            self.diary[label] = self.final if label == "Union Station" else find(label)
        self.obs = {label: None for label in DIARY_STOPS}  # label -> delay minutes or "n/a"
        self.checkpoints = []
        if alert:
            for label in CHECKPOINTS:
                st = find(label)
                if st and self.watch and self.mine and \
                        int(self.watch["stop_sequence"]) < int(st["stop_sequence"]) < int(self.mine["stop_sequence"]):
                    self.checkpoints.append((label, st))
                else:
                    log(f"Warning: checkpoint '{label}' not on #{num} between {WATCH_STOP} and {MY_STOP}.")
        self.last_pred = {}  # stop_id -> predicted epoch seconds
        self.polls = self.polls_tu = self.polls_vp = 0
        self.last_seen = ""  # most recent raw feed summary, for troubleshooting
        self.seen_live = self.seen_pred = self.canceled = self.done = False
        self.alert_state = "waiting" if alert else "off"
        self.alert_sent_exp = None
        self.start = gtfs_time(day, self.first["departure_time"]) - timedelta(minutes=15)
        self.deadline = gtfs_time(day, self.final["arrival_time"]) + timedelta(minutes=60)

    # -- helpers
    def sched(self, st, kind="departure_time"):
        return gtfs_time(self.day, st[kind])

    def useq(self, u):
        return u.stop_sequence or self.seq_of.get(u.stop_id, 0)

    def cur_seq(self, vp):
        if vp is None:
            return None
        return vp.current_stop_sequence if vp.HasField("current_stop_sequence") else self.seq_of.get(vp.stop_id)

    def has_left(self, st, tu, vp, now):
        """Any one of three independent signals means the train has left this stop.
        None of them is allowed to veto the others: a stale or oddly-numbered GPS
        position must not suppress a perfectly good prediction, and vice versa."""
        s = int(st["stop_sequence"])
        # 1) GPS reports a position past this stop
        cur = self.cur_seq(vp)
        if cur and cur > s:
            return True
        if tu is not None and tu.stop_time_update:
            # 2) this stop has dropped out of the predictions (feeds drop passed stops)
            seqs = [x for x in (self.useq(u) for u in tu.stop_time_update) if x]
            if seqs and all(x > s for x in seqs):
                return True
            # 3) the predicted departure time for this stop has passed
            t = self.last_pred.get(st["stop_id"])
            if t and now.timestamp() > t + 60:
                return True
        # 4) last resort: we have live data for this train and the scheduled departure
        # is well past, with no prediction saying otherwise
        if (tu is not None or vp is not None) and not self.last_pred.get(st["stop_id"]) \
                and now > self.sched(st) + timedelta(minutes=LATE_FALLBACK_MIN):
            log(f"#{self.num}: no prediction for {st['stop_id']}; treating as departed "
                f"{LATE_FALLBACK_MIN} min after scheduled time.")
            return True
        return False

    def reached_final(self, tu, vp):
        s = int(self.final["stop_sequence"])
        cur = self.cur_seq(vp)
        return bool(cur) and (cur > s or (cur == s and vp.current_status == STOPPED_AT))

    def expected_arrival(self, st, tu):
        """(scheduled, expected, is_live) arrival at `st`."""
        sched = self.sched(st, "arrival_time")
        if tu is None:
            return sched, sched, False
        my_seq = int(st["stop_sequence"])
        last_delay = None
        for u in sorted(tu.stop_time_update, key=self.useq):
            seq = self.useq(u)
            ev = u.arrival if u.HasField("arrival") else u.departure
            if seq == my_seq:
                if u.schedule_relationship == rt.TripUpdate.StopTimeUpdate.SKIPPED:
                    return sched, None, True
                if ev.time:
                    return sched, datetime.fromtimestamp(ev.time, TZ), True
                if ev.HasField("delay"):
                    return sched, sched + timedelta(seconds=ev.delay), True
            if seq and seq < my_seq:
                if ev.HasField("delay"):
                    last_delay = ev.delay
                elif ev.time and seq in self.by_seq:
                    last_delay = ev.time - self.sched(self.by_seq[seq], "arrival_time").timestamp()
        if last_delay is not None:
            return sched, sched + timedelta(seconds=last_delay), True
        if tu.HasField("delay"):
            return sched, sched + timedelta(seconds=tu.delay), True
        return sched, sched, False

    def remember_predictions(self, tu):
        if tu is None:
            return
        for u in tu.stop_time_update:
            st = self.by_seq.get(self.useq(u))
            if st is None:
                continue
            ev = u.departure if u.HasField("departure") else u.arrival
            if ev.time:
                self.last_pred[st["stop_id"]] = ev.time
            elif ev.HasField("delay"):
                self.last_pred[st["stop_id"]] = self.sched(st).timestamp() + ev.delay
            else:
                continue
            self.seen_pred = True

    def record(self, label, st, now, final=False):
        kind = "arrival_time" if final else "departure_time"
        t = self.last_pred.get(st["stop_id"])
        actual = datetime.fromtimestamp(t, TZ) if t else now
        self.obs[label] = round((actual - self.sched(st, kind)).total_seconds() / 60)
        log(f"#{self.num} {'reached' if final else 'left'} {label}: {self.obs[label]} min "
            f"({'Metra prediction' if t else 'GPS detection'})")
        # anything earlier we missed (e.g. the run started late) gets filled from predictions
        for lab, s in self.diary.items():
            if s and self.obs[lab] is None and int(s["stop_sequence"]) < int(st["stop_sequence"]):
                t2 = self.last_pred.get(s["stop_id"])
                self.obs[lab] = round((datetime.fromtimestamp(t2, TZ) - self.sched(s)).total_seconds() / 60) \
                    if t2 else "n/a"

    # -- alert emails (train #1236 only)
    def build_message(self, tu, checkpoint=None):
        sched, exp, live = self.expected_arrival(self.mine, tu)
        if exp is None:
            msg = f"⚠️ BNSF #{self.num} is now shown SKIPPING {MY_STOP} today."
            reason = delay_reason(self.trip_id, self.pat)
            return msg + (f"\nReason: {reason}" if reason else ""), None
        late, status = status_text(sched, exp)
        head = (f"🚆 BNSF #{self.num} just left {WATCH_STOP}." if checkpoint is None else
                f"🚆 UPDATE: BNSF #{self.num} left {checkpoint} running {status}.")
        msg = f"{head}\nExpected at {MY_STOP}: {fmt(exp)} (scheduled {fmt(sched)}, {status})."
        msg += ("\nSource: Metra live prediction." if live else
                "\nSource: SCHEDULE ONLY. Metra isn't publishing a live prediction for this train right now.")
        if late > DELAY_THRESHOLD_MIN:
            reason = delay_reason(self.trip_id, self.pat)
            msg += f"\nLikely reason: {reason}" if reason else "\nMetra hasn't posted a reason yet."
        return msg, exp

    def alert_step(self, tu, vp, now):
        if self.alert_state == "waiting":
            if self.has_left(self.watch, tu, vp, now):
                body, exp = self.build_message(tu)
                send_sms(body)
                self.alert_state = "tracking" if exp else "done"
            elif tu is None and vp is None and now > self.sched(self.watch) + timedelta(minutes=NO_DATA_GRACE_MIN):
                send_sms(f"ℹ️ No live tracking for BNSF #{self.num} right now. Metra treats that as "
                         f"on schedule: {MY_STOP} at {fmt(self.sched(self.mine, 'arrival_time'))}.")
                self.alert_state = "done"
        elif self.alert_state == "tracking":
            if self.has_left(self.mine, tu, vp, now):
                self.alert_state = "done"
                return
            sched, exp, live = self.expected_arrival(self.mine, tu)
            if exp is None:
                send_sms(self.build_message(tu)[0])
                self.alert_state = "done"
                return
            while self.checkpoints and self.has_left(self.checkpoints[0][1], tu, vp, now):
                label, _ = self.checkpoints.pop(0)
                late, status = status_text(sched, exp)
                if live and late > DELAY_THRESHOLD_MIN:
                    send_sms(self.build_message(tu, checkpoint=label)[0])
                else:
                    log(f"#{self.num} left {label} {status}: no update email needed.")

    # -- one poll
    def step(self, snap, now):
        if self.done or now < self.start:
            return
        tus, vps = snap
        tu = next((x for x in tus if is_trip(x.trip, self.trip_id, self.pat)), None)
        vp = next((x for x in vps if is_trip(x.trip, self.trip_id, self.pat)), None)
        if tu is not None or vp is not None:
            self.seen_live = True
        self.remember_predictions(tu)

        cur = self.cur_seq(vp)
        pos = (f"seq={cur} {rt.VehiclePosition.VehicleStopStatus.Name(vp.current_status)}"
               if vp is not None else "no GPS")
        self.polls += 1
        self.polls_tu += tu is not None
        self.polls_vp += vp is not None
        upd = ""
        if tu is not None:
            upd = " | updates: " + ", ".join(
                f"{u.stop_id or self.useq(u)}@"
                f"{fmt(datetime.fromtimestamp(u.departure.time or u.arrival.time, TZ)) if (u.departure.time or u.arrival.time) else '?'}"
                for u in list(tu.stop_time_update)[:4])
        self.last_seen = f"live_update={'yes' if tu is not None else 'no'} | {pos}{upd}"
        log(f"#{self.num}: {self.last_seen}")

        if tu is not None and tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
            self.canceled = True
            if self.alert and self.alert_state != "done":
                reason = delay_reason(self.trip_id, self.pat)
                send_sms(f"❌ Metra shows BNSF #{self.num} CANCELED today."
                         + (f"\nReason: {reason}" if reason else ""))
            self.done = True
            return

        for label, st in self.diary.items():
            if st is None or self.obs[label] is not None:
                continue
            if st is self.final:
                if self.reached_final(tu, vp):
                    self.record(label, st, now, final=True)
                elif tu is None and vp is None and self.seen_live and \
                        all(self.obs[l] is not None for l, s in self.diary.items() if s and s is not self.final):
                    # train dropped off the feed right after its last stop = it arrived
                    self.record(label, st, now, final=True)
            elif self.has_left(st, tu, vp, now):
                self.record(label, st, now)

        if self.alert:
            self.alert_step(tu, vp, now)

        diary_done = all(self.obs[l] is not None for l, s in self.diary.items() if s)
        if (diary_done and self.alert_state in ("off", "done")) or now > self.deadline:
            self.done = True

    def finish(self):
        if self.alert and self.alert_state == "waiting":
            t = self.last_pred.get(self.watch["stop_id"])
            send_sms(
                f"⚠️ BNSF #{self.num} never reported leaving {WATCH_STOP}.\n"
                f"Diagnostics: {self.polls} checks | trip updates seen on {self.polls_tu} | "
                f"GPS seen on {self.polls_vp} | last prediction for {WATCH_STOP}: "
                f"{fmt(datetime.fromtimestamp(t, TZ)) if t else 'none'}\n"
                f"Last feed read: {self.last_seen or 'nothing'}\n"
                f"Scheduled {WATCH_STOP} departure was {fmt(self.sched(self.watch))}.")

    def row(self):
        vals, nums = [], []
        for label in DIARY_STOPS:
            st, v = self.diary[label], self.obs[label]
            if st is None:
                vals.append("doesn't stop")
            elif v is None or v == "n/a":
                vals.append("")
            else:
                vals.append(v)
                nums.append(v)
        max_delay = max(nums) if nums else ""
        if self.canceled:
            status = "Canceled"
        elif not self.seen_live:
            status = "No live data"
        elif not nums:
            status = "Incomplete"
        else:
            status = "Late" if max_delay > DELAY_THRESHOLD_MIN else "On time"
        reason = ""
        if self.canceled or (nums and max_delay > DELAY_THRESHOLD_MIN):
            reason = delay_reason(self.trip_id, self.pat) or "none posted"
        source = ("Metra live predictions" if self.seen_pred else
                  "GPS only" if self.seen_live else "none")
        aurora = self.diary["Aurora"]
        return [self.day.isoformat(), self.day.strftime("%a"), f"#{self.num}",
                fmt(self.sched(aurora)) if aurora else "",
                fmt(self.sched(self.mine, "arrival_time")) if self.mine else ""] \
            + vals + [max_delay, status, reason, source]


# ---------------------------------------------------------------- email + sheet
def send_sms(body, dry=False):
    """Send an alert. Default: email through your own Gmail (free)."""
    log("ALERT:\n" + body)
    if dry:
        return
    provider = os.getenv("SMS_PROVIDER", "email").lower()
    if provider == "email":
        sender = os.environ["GMAIL_ADDRESS"].strip()
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = os.getenv("EMAIL_TO") or sender
        lines = body.splitlines()
        # For train alerts, the expected-arrival line is the most useful subject.
        msg["Subject"] = (("[Dry run] " if body.startswith("[Dry run]") else "")
                          + ("UPDATE: " if "UPDATE" in lines[0] else "") + lines[1]
                          if "🚆" in lines[0] and len(lines) > 1 else lines[0])
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, re.sub(r"\s", "", os.environ["GMAIL_APP_PASSWORD"]))
            smtp.send_message(msg)
    else:
        raise RuntimeError(f"Unknown SMS_PROVIDER '{provider}'")
    log("Alert sent.")


def write_diary(rows):
    """rows: list of (tab name, values). Sent to the Apps Script attached to your sheet."""
    url = os.getenv("SHEET_WEBHOOK_URL", "").strip()
    if not url:
        log("SHEET_WEBHOOK_URL not set; skipping diary.")
        return
    payload = {"headers": HEADERS, "rows": [{"tab": tab, "values": vals} for tab, vals in rows]}
    r = requests.post(url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=60)
    if not r.ok or '"ok":true' not in r.text.replace(" ", ""):
        raise RuntimeError(f"Google Sheet didn't accept the diary rows (HTTP {r.status_code}): {r.text[:200]}")
    log(f"Wrote {len(rows)} diary row(s) to the Google Sheet.")


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
    ap.add_argument("--test-sheet", action="store_true")
    args = ap.parse_args()

    if args.test_sms:
        send_sms(f"✅ Test from your Metra #{ALERT_TRAIN} watcher. Alerts are working!")
        return
    if args.test_sheet:
        today = datetime.now(TZ).date()
        write_diary([("Test", [today.isoformat(), today.strftime("%a"), "#TEST", "", "",
                               "", "", "", "", "", "", "Test row", "It works! You can delete this tab.", ""])])
        send_sms("✅ Test row written to your Metra diary sheet (see the 'Test' tab).")
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
    order = [ALERT_TRAIN] + [t for t in DIARY_TRAINS if t != ALERT_TRAIN]
    trains, not_running = [], []
    for num in order:
        trip = find_trip(g, today, num)
        if trip is None:
            not_running.append(num)
            continue
        t = Train(g, today, num, trip, alert=(num == ALERT_TRAIN))
        if t.alert and (not t.watch or not t.mine):
            sys.exit(f"Couldn't find '{WATCH_STOP}' or '{MY_STOP}' on #{num} ({t.trip_id}).")
        trains.append(t)
        log(f"#{num} ({t.trip_id}): first stop {fmt(t.sched(t.first))}, "
            f"{MY_STOP} {fmt(t.sched(t.mine, 'arrival_time')) if t.mine else 'no stop'}, "
            f"Union Station {fmt(t.sched(t.final, 'arrival_time'))}")

    if args.dry_run:
        snap = snapshot()
        lines = []
        for t in trains:
            tu = next((x for x in snap[0] if is_trip(x.trip, t.trip_id, t.pat)), None)
            vp = next((x for x in snap[1] if is_trip(x.trip, t.trip_id, t.pat)), None)
            live = "live data NOW" if (tu or vp) else "no live data right now"
            aur = t.diary["Aurora"]
            lines.append(f"#{t.num}: Aurora {fmt(t.sched(aur)) if aur else 'no stop'}, "
                         f"{MY_STOP} {fmt(t.sched(t.mine, 'arrival_time')) if t.mine else 'no stop'} ({live})")
        for num in not_running:
            lines.append(f"#{num}: not scheduled today")
        send_sms("[Dry run] Metra watcher found these trains:\n" + "\n".join(lines))
        return

    alert_ok = any(t.alert for t in trains)
    if not alert_ok:
        send_sms(f"ℹ️ BNSF #{ALERT_TRAIN} is not scheduled today (holiday or schedule change). No train to watch.")

    if trains:
        start = min(t.start for t in trains)
        wait = (start - datetime.now(TZ)).total_seconds()
        if wait > 0:
            log(f"Sleeping until {fmt(start)}...")
            time.sleep(wait)

        while not all(t.done for t in trains):
            now = datetime.now(TZ)
            try:
                snap = snapshot()
            except Exception as e:
                log("Feed error, retrying:", e)
                if all(now > t.deadline for t in trains):
                    break
                time.sleep(POLL_SECONDS)
                continue
            for t in trains:
                t.step(snap, now)
            time.sleep(POLL_SECONDS)

        for t in trains:
            t.finish()

    rows = [(f"Train {t.num}", t.row()) for t in trains]
    rows += [(f"Train {num}", [today.isoformat(), today.strftime("%a"), f"#{num}", "", ""]
              + [""] * len(DIARY_STOPS) + ["", "Not scheduled", "", ""]) for num in not_running
             if num in DIARY_TRAINS]
    write_diary(rows)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Always email, even when something breaks.
        try:
            send_sms(f"⚠️ Metra watcher hit an error today: {type(e).__name__}: {e}")
        except Exception:
            pass
        raise
