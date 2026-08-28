# Putting this on GitHub and making it downloadable

Written for someone who has never used GitHub. Follow it in order.

You do **not** need a Mac. GitHub builds the Mac version for you on a real Mac
in the cloud, free.

---

## Decide this first: public or private?

It changes who can download the app, so get it right before you start.

| | Who can download |
|---|---|
| **Private repo** | Only people you explicitly invite, and they need a GitHub account |
| **Public repo** | Anybody with the link, no account needed |

This is lab software. The program it replaces carries somebody's copyright line
and belongs to a named research project. **Start private.** It gives you version
history, backup, and a download link for colleagues. You can switch to public in
two clicks later if the group decides that's fine. Going the other way is much
harder, because anything public may already have been copied.

If you need a link that works for someone without a GitHub account and the repo
is private, email them the file directly instead of making the repo public.

---

## No Git installed? Do the whole thing in your browser

You do not need Git, PyCharm, or any command line. Skip to the browser method
below and ignore Parts 1 to 3.

**Step A.** On github.com click the **+** top right, then **New repository**.
Name it `pump-control`, choose **Private**, click **Create repository**.

**Step B.** On the empty repo page click **uploading an existing file**. Open
`C:\Pump Software` in Explorer and drag these in:

```
pump_app.py          wm323.py          sequence.py
test_connection.py   requirements.txt  README.md
SETUP.md             RELEASING.md      build_exe.bat
build_mac.command    RUN_ME.bat        TEST_CONNECTION.bat
```

**Do not upload** the `logs` folder, the `.venv` folder, `__pycache__`, or any
`.xlsx` spreadsheet. Those are run output, program plumbing and experimental
data. `.gitignore` only keeps those out when you use Git properly, so when
dragging files in by hand it is on you to leave them out.

Scroll down, click **Commit changes**.

**Step C.** The build file lives in a hidden folder Windows will not show you,
so create it in the browser instead. Click **Add file > Create new file**. In
the name box type exactly:

```
.github/workflows/build.yml
```

Typing the slashes creates the folders automatically. Paste in the contents of
`build.yml`, scroll down, **Commit changes**.

**Step D.** Click **Releases** on the right of the repo page, then **Create a
new release**. Click **Choose a tag**, type `v1.0`, click **Create new tag**.
Title `v1.0`. Click **Publish release**.

**Step E.** Click the **Actions** tab. A build is running. Give it five
minutes, then go back to Releases and refresh. `PumpControl.exe` and
`PumpControl-mac.zip` are now attached.

For the next version, repeat Step D with `v1.1`.

---

## Part 1: Create the repository

1. Make a GitHub account at **github.com** if you don't have one.
2. In PyCharm: **Git > GitHub > Share Project on GitHub**.
3. Log in when prompted.
4. Repository name: `pump-control`. **Tick Private.**
5. It shows a list of files it's about to upload. Check that `logs/`, `.venv/`
   and any `.xlsx` are **not** in the list. The `.gitignore` should already be
   keeping them out. If you see experimental data in that list, cancel and ask
   before continuing.
6. Commit message: `initial commit`. Click **Share**.

Your code is now on GitHub. From here, `Ctrl+K` commits changes and
`Ctrl+Shift+K` pushes them up.

---

## Part 2: Check the build file went up

Open your repo on github.com. You should see a folder called `.github`.

If it isn't there, PyCharm hid it because the name starts with a dot. Fix it in
the Terminal (Alt+F12):

```
git add -f .github/workflows/build.yml
git commit -m "add build workflow"
git push
```

Refresh the page. It should appear.

---

## Part 3: Build both apps

This is the whole point. One command produces a Windows .exe and a Mac .app.

In the PyCharm Terminal:

```
git tag v1.0
git push origin v1.0
```

That's it. A version tag is what triggers the build.

Now go to your repo on github.com and click the **Actions** tab. You'll see a
run in progress with three jobs: `windows`, `macos`, `release`. It takes about
five minutes. Green ticks mean it worked.

If something goes red, click into it. The log shows the exact error. The usual
cause is a typo in a filename.

---

## Part 4: Get the downloads

Click the **Releases** link on the right-hand side of your repo page, or go to
the **Code** tab and look for "Releases".

You'll see `v1.0` with the built apps attached: `PumpControl.exe` for
Windows, plus a zip for each kind of Mac.

Those are permanent download links. Send them to whoever needs the app.

For the next version, change the code, commit, push, then:

```
git tag v1.1
git push origin v1.1
```

Both apps rebuild automatically. The old version stays available, which matters
if you ever need to know which version produced a given run.

---

## Part 5: What the person downloading it has to do

You get three files on the release page. Send people the right one.

| File | For |
|---|---|
| `PumpControl.exe` | Windows |
| `PumpControl-mac-AppleSilicon.zip` | Macs from 2020 onwards (M1, M2, M3, M4) |
| `PumpControl-mac-Intel.zip` | Older Intel Macs |

To check which Mac someone has: Apple menu, About This Mac. It says either
"Apple M..." or "Intel".

**Windows.** Download the .exe, put it in a normal folder like
`C:\PumpControl`, double-click.

- SmartScreen will say "Windows protected your PC". Click **More info** then
  **Run anyway**. That happens because the app is not code-signed, which costs
  a few hundred pounds a year. It is not a virus warning, though it looks like
  one.
- Antivirus may quarantine it. Single-file PyInstaller apps trip heuristics
  because self-extracting-then-running is also what malware does.

**Mac.** Download the zip, double-click to unzip, drag `PumpControl.app` to
Applications.

- **First launch: right-click the app and choose Open**, then click Open in the
  dialog that appears. Double-clicking normally will just say the developer
  cannot be verified and refuse to run. Once done, it launches normally
  forever after.
- If macOS still refuses, open Terminal and run:
  `xattr -dr com.apple.quarantine /Applications/PumpControl.app`
- The pump shows up as something like `/dev/tty.usbserial-1420` rather than
  `COM4`. The port dropdown handles that, but you will probably need the macOS
  driver for your specific USB-to-serial adapter, most often Prolific or FTDI.
- Reports go to a `logs` folder next to the app. If the app is in
  `/Applications` and that is not writable, they land in
  `~/Documents/PumpControl logs` instead. The activity log always states the
  full path.

---

## If the Mac builds fail

They cannot break your Windows release. The Mac job is marked
`continue-on-error`, and the release step attaches whatever actually got built,
warning about anything missing. Worst case you get the Windows app and a yellow
tick instead of a green one.

The likeliest cause is GitHub retiring a runner image. `macos-13` went in
December 2025, which is why this uses `macos-15-intel` instead. That one is
scheduled to disappear in August 2027, after which GitHub drops Intel macOS
entirely. When that happens, delete these three lines from `build.yml`:

```yaml
          - runner: macos-15-intel
            arch: Intel
```

---

## Nobody has tested the Mac build

Worth saying plainly. The code has no Windows-specific parts, so it should
work, but "should" is carrying real weight there. Before handing the Mac
version to anyone, somebody needs to open it on an actual Mac, connect to an
actual pump, and check a report file gets written. Until that happens, treat
the Windows build as the real one.
