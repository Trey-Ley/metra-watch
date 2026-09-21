# Metra #1236 "Left Aurora" Alert

Every weekday, this watches Metra's official live feed for BNSF train #1236.
The moment it leaves Aurora, you get an email like:

> 🚆 BNSF #1236 just left Aurora.
> Expected at Lisle: 7:41 AM (scheduled 7:32 AM, 9 min late).
> Likely reason: Freight train interference near Eola

It also emails you if the train is canceled or will skip Lisle. It runs for free on
GitHub's servers, so your computer doesn't need to be on. No coding needed, about 20 minutes of setup.

---

## Step 1: Get a free Metra API key
1. Go to https://metra.com/developers
2. Accept the license agreement and fill in the form.
3. Metra emails you a key (may take a day or two). Keep it handy.

## Step 2: Let the tool email you from your own Gmail (free)
The alert is emailed from your Gmail account to itself, so it costs nothing.
Google requires a special "app password" for this (your normal password won't work):
1. Turn on 2-Step Verification if it isn't already: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Type a name like `Metra watcher` and click **Create**.
4. Copy the 16-letter password it shows (you only see it once).

Tip: turn on Gmail app notifications on your phone. The email's subject line is the key info
(e.g. "Expected at Lisle: 7:41 AM (scheduled 7:32 AM, 9 min late)"), so you can read it on your lock screen.

## Step 3: Put these files on GitHub
1. Create a free account at https://github.com
2. Click **+** (top right) → **New repository**. Name it `metra-watch`, choose **Private**, click **Create repository**.
3. Click **uploading an existing file** and drag in `metra_alert.py`, `requirements.txt`, and `README.md`. Click **Commit changes**.
4. Click **Add file → Create new file**. For the name type exactly: `.github/workflows/metra.yml`
   Paste in the contents of `metra.yml`, then click **Commit changes**.

## Step 4: Add your secret keys
In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `METRA_API_KEY` | your Metra key |
| `GMAIL_ADDRESS` | `tr3y.l3hman@gmail.com` |
| `GMAIL_APP_PASSWORD` | the 16-letter app password from Step 2 |

(Optional: add `EMAIL_TO` to send the alert to a different address.)

## Step 5: Test it
Go to the **Actions** tab → **Metra train watcher** → **Run workflow**.
1. Run with **test-sms**. You should get a test email within a minute.
2. Run with **dry-run**. Click into the run and open the "Watch train" step. You should see a line like
   `scheduled Aurora 7:0x AM, Lisle 7:32 AM`. That confirms it found the right train.
   (During the morning commute it will also show live status.)

That's it. It runs automatically at 6:05 AM every weekday, waits for the train, and emails you.

---

## Good to know
- **Holidays:** if #1236 isn't running (e.g., a Metra holiday schedule), it quietly does nothing.
- **No GPS signal:** if Metra has no live data 10 minutes after the scheduled Aurora departure,
  you get an email saying so and the scheduled Lisle time (that's how Metra treats missing data).
- **Delay reasons** come from Metra's posted service alerts. If Metra hasn't posted one yet,
  the email says so. Line-wide alerts are labeled as "may be related."
- **GitHub's scheduler** occasionally starts jobs late when its servers are busy. The script
  starts ~1 hour early to absorb this, but on rare days a run could be skipped.
- **Keep it alive:** GitHub pauses schedules on repos with no activity for 60 days. If you get
  an email about that, click the button in the email to re-enable it.
- **Change settings** by editing the top of `metra_alert.py`: train number, stations, or the
  5-minute delay threshold.
