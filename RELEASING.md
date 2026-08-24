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

You'll see `v1.0` with two files attached:

- **PumpControl.exe** for Windows
- **PumpControl-mac.zip** for Mac

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

**Windows.** Download `PumpControl.exe`, put it in a normal folder like
`C:\PumpControl`, double-click.

- Windows SmartScreen will say "Windows protected your PC". Click **More info**
  then **Run anyway**. This happens because the app isn't code-signed, which
  costs a few hundred pounds a year. It is not a virus warning.
- Antivirus may quarantine it outright. PyInstaller single-file apps trip
  heuristics because self-extracting-then-running is also what malware does.
- **Don't put it in Program Files.** It writes report files next to itself and
  that folder needs admin rights.

**Mac.** Download `PumpControl-mac.zip`, double-click to unzip, drag
`PumpControl.app` to Applications.

- **First launch: right-click the app and choose Open**, then click Open in the
  dialog. Double-clicking it normally will just say the developer cannot be
  verified and refuse. You only need to do this once.
- If macOS refuses even then, open Terminal and run:
  `xattr -dr com.apple.quarantine /Applications/PumpControl.app`
- The Mac build is Apple Silicon (M1 and later). On an older Intel Mac it will
  either not launch or run slowly through Rosetta.
- The pump appears as something like `/dev/tty.usbserial-XXXX` rather than
  `COM4`. The port dropdown handles that automatically, but you'll likely need
  the driver for your specific USB-to-serial adapter.

---

## Is a Mac version actually useful?

Worth asking. The pump lives in a lab and lab machines are usually Windows.
Nothing here is Windows-specific so the Mac build costs nothing to produce, but
if nobody is going to run it on a Mac, don't spend time testing that path. The
Windows build is the one that matters.

Whichever you ship, **test the built app before sending it to anyone**. Freezing
the code into an executable exercises paths the script version never touches,
and the path handling for report files is one of them.
