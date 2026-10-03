# End-to-end testing on Windows and macOS

Everything in this repository has been tested on Linux. On Windows and macOS,
the parts that differ by platform have **never run**:

- the desktop backends: UI Automation on Windows, AXUIElement on macOS;
- the screen backends: win32 on Windows, Quartz on macOS;
- the Escape-twice key listener;
- the managed-policy readers;
- the Windows installer.

This guide takes one person through them on each platform, from the automated
tests to the whole product in a browser. It takes about an hour per machine.
Nothing here needs a model provider: the scripted `demo` profile stands in, and
every event it writes is labelled "scripted". The one optional step that needs
a real model says so.

Run the parts in order. A failure early on explains failures later. Section 7
lists what to send back.

| Part | What it proves | Windows | macOS |
| --- | --- | --- | --- |
| 1. Set up | The agent installs | ✓ | ✓ |
| 2. Automated tests | Everything platform-neutral passes here | ✓ | ✓ |
| 3. Platform check | Desktop and screen backends, and the key listener, against a native app | ✓ | ✓ |
| 4. The product, end to end | Files, browser, connectors, approvals, consent, restart, workflows, revocation | ✓ | ✓ |
| 5. Desktop and screen with a real model | Optional; needs an API key | ✓ | ✓ |
| 6. Deployment | Managed policy, enrollment, and on Windows the installer | ✓ | policy only |

The **windows** workflow in GitHub Actions (Actions, windows, Run workflow)
runs parts 2 and 6's installer on a Windows server with no desktop session. It
is worth running too, but it cannot replace parts 3 to 5, which need a real
desktop with windows on it.

---

## 1. Set up

You need Git, Node 22 or later, and Python 3.12. The control plane, web app and
agent all run on the same computer.

### Windows (PowerShell)

```powershell
winget install Git.Git OpenJS.NodeJS.LTS Python.Python.3.12 UB-Mannheim.TesseractOCR
# Open a new PowerShell so PATH picks them up, then:
git clone https://github.com/dandan002/ondo-quartermaster.git
cd ondo-quartermaster
npm install
npx playwright install chromium
cd agent
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev,desktop]"
$env:Path += ";C:\Program Files\Tesseract-OCR"     # if tesseract is not found
tesseract --version
```

### macOS (Terminal)

```sh
brew install git node python@3.12 tesseract
git clone https://github.com/dandan002/ondo-quartermaster.git
cd ondo-quartermaster
npm install
npx playwright install chromium
cd agent
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,desktop]"
tesseract --version
```

On macOS, grant the terminal app (Terminal, iTerm, or your editor if you run
from it) three permissions in **System Settings, Privacy & Security**:

- **Accessibility**: reading and acting on windows;
- **Screen Recording**: screenshots;
- **Input Monitoring**: the Escape-twice listener.

Quit and reopen the terminal after granting them. macOS does not let a program
grant these to itself, and asks again after an update to the terminal app.

In the rest of this guide, `ondo-agent` means `.venv\Scripts\ondo-agent` on
Windows and `.venv/bin/ondo-agent` on macOS, run from the `agent` folder.

---

## 2. Automated tests

From the `agent` folder:

```powershell
.venv\Scripts\python -m pytest -q        # Windows
```
```sh
.venv/bin/python -m pytest -q            # macOS
```

**Expect:** everything passes except the desktop and screen tests
(`test_desktop.py`, `test_screen.py`), which **skip**. They need a Linux virtual
desktop; part 3 covers the same ground on this machine. The integration tests
start their own control plane with `npx tsx`, so `npm install` must have worked.

**Record:** the last line (for example `58 passed, 11 skipped`), and the full
output of every failure. The likely places are:

- file paths, where `\` meets `/`;
- file permissions on consents and credentials;
- starting the sample MCP servers.

---

## 3. Platform check against a native app

`ondo-agent platform-check` runs, against one window you name:

- the desktop backend;
- the screen backend;
- local OCR;
- the Escape-twice listener.

It prints `PASS`, `FAIL` or `SKIP` for each check. It reads only the window
you name. With `--type`, it replaces the text in that window's first text
field. It saves one screenshot to `--out`, and never sends it anywhere.

### Windows

1. Open **Notepad**, make sure it has an empty tab, and leave it visible, not
   minimised.
2. Run:
   ```powershell
   ondo-agent platform-check --window Notepad --type "Ondo platform check 4471" --out check-output
   ```
3. Then, as a second app with a different kind of tree, open **Calculator** and
   run:
   ```powershell
   ondo-agent platform-check --window Calculator --out check-output
   ```

### macOS

1. Open **TextEdit** with a new blank document, and leave it visible.
2. Run:
   ```sh
   ondo-agent platform-check --window TextEdit --type "Ondo platform check 4471" --out check-output
   ```
3. Then open **Calculator** and run:
   ```sh
   ondo-agent platform-check --window Calculator --out check-output
   ```

### What each line means

| Check | Passes when | If it fails |
| --- | --- | --- |
| desktop backend | The backend loads (`UiaBackend` on Windows, `AxBackend` on macOS) | macOS: Accessibility is not granted. Windows: `pywinauto` is not installed. |
| list windows | Your open windows are listed | The backend cannot enumerate windows. |
| find window | The named window is found | Check the name, and that it is not minimised. |
| read accessibility tree | Elements with names are listed | The app exposes little; note which app. |
| set text by element | The text is written into the field and reads back | The key test for acting "by element name, not coordinates". |
| screenshot | The window is captured at its real size | macOS: Screen Recording is not granted. A window covering it also makes this fail, on purpose. |
| OCR | The text in the screenshot is read, including what `--type` wrote | Tesseract is missing or not on PATH (SKIP), or the capture is wrong. |
| escape-twice listener | The key listener starts and stops | macOS: Input Monitoring is not granted. Without it, the agent turns input control off. |

**Also check by eye:**

- Open `check-output/platform-check.png`. It should show exactly the window,
  not a shifted or cropped area. A mismatch means a scaling bug, which is most
  likely on a Retina or 125–150% scaled display.
- Say what your display scaling is (Windows: Settings, Display, Scale; macOS:
  Retina or not).
- If you have a second monitor, move the window there and run the check again.

---

## 4. The product, end to end

This part runs everything together: the control plane, the web app and the
agent, with the scripted `demo` model.

### 4.1 Start it

Use three terminals, all from the repository root.

**Terminal 1, the control plane:**
```powershell
cd server; $env:ONDO_DEV="1"; $env:ONDO_SEED="demo"; npx tsx src/main.ts       # Windows
```
```sh
cd server && ONDO_DEV=1 ONDO_SEED=demo npx tsx src/main.ts                     # macOS
```

**Terminal 2, the web app:** `npm run dev --workspace web`, then open
http://localhost:5173.

**Terminal 3, the agent's sample data and configuration** (from `agent`):
```powershell
ondo-agent demo-data demo
copy ondo.example.yaml ondo.yaml
Start-Process -NoNewWindow .venv\Scripts\ondo-agent -ArgumentList "portal"     # the sample billing portal
$env:ONDO_MODEL_PROFILE="demo"
```
```sh
ondo-agent demo-data demo
cp ondo.example.yaml ondo.yaml
ondo-agent portal &                                                             # the sample billing portal
export ONDO_MODEL_PROFILE=demo
```

In `ondo.yaml`, set `desktop: enabled: true` and `screen: enabled: true`. The
connectors are already configured, with the sample servers. If the browser
steps below cannot find Chromium, set `ONDO_CHROMIUM` to the path of the
Chromium that `npx playwright install chromium` downloaded.

### 4.2 Sign in and pair

1. In the browser, sign in as `mara.okonjo@northwind-ops.com` with the password
   `quartermaster-demo`. The verification code is in the development outbox,
   linked from the Verify screen.
2. On the Pair screen, copy the code. In terminal 3, run:
   ```
   ondo-agent pair --server http://localhost:8787 --code 123-456
   ondo-agent connect
   ```
   Leave `connect` running.
3. **Grant** these three:
   - **Read files in the folders you name**: the folder
     `agent/demo/Northwind client drive` (its full path);
   - **Type and click for you**;
   - **See the screen while you work**: Notepad or TextEdit.

**Expect:** the rail shows the desktop agent as **Connected**, and the Files
page lists the folder.

### 4.3 Tasks to run

Start each one from **New task** (or Ctrl/⌘+K), using exactly this wording: the
scripted model picks its script by the words. After each, write down whether
it did what the **Expect** line says.

1. **Files.** *Build the Q3 renewal pack for Northwind from the contracts in the client folder, and flag anything that uplifts above five per cent.*
   **Expect:** it reads the contracts, then stops for approval with the exact
   changed values before writing the workbook. Approve it; the workbook is
   written. The payroll file never appears (it is excluded by policy).
2. **A question.** In the assistant panel: *Why did row 14 not match the contract?*
   **Expect:** an answer naming the customer and the uplift, with nothing
   written.
3. **The browser.** *Key the Q3 renewal changes from the signed contracts into the billing portal. Stop before submitting.*
   **Expect:** Chromium opens the sample portal and fills it in. The submit
   stops for approval.
4. **Ticketing, and consent.** *Reply to Halleck's renewal ticket with the new annual value from the signed contract, and mark it as waiting on the customer.*
   **Expect, in this order:**
   - a **Let Ondo use Ticketing?** card;
   - an approval showing the exact reply text and who receives it;
   - an approval showing the status change, open → pending.

   **Files and connections** then shows Ticketing as allowed by you, with
   **Revoke**.
5. **Mail and calendar.** *Answer Priya's email with the renewal value from the signed contract, and offer her a 30-minute call next week.*
   **Expect:**
   - two consent cards, one for Mail and one for Calendar;
   - the reply's approval, with Priya's address flagged as outside
     northwind-ops.com;
   - an approval for the invitation.
6. **A long task that survives a restart.** *Reconcile receivables for September.*
   Allow Ledger. While the step says *Reconciling receivables for 2026-09: …%
   done*:
   - stop the agent (Ctrl+C in terminal 3);
   - wait ten seconds;
   - start `ondo-agent connect` again.

   **Expect:**
   - the same task carries on;
   - the timeline says the desktop agent restarted;
   - it finishes with *3 items did not match*.

   The sample reconciliation takes 20 seconds. For longer, set it in
   `ondo.yaml`, under `connectors`, `ledger`: `env: {ONDO_LEDGER_SECONDS: "120"}`.
   A variable set in the shell does not reach it: connector servers get only a
   safe subset of the environment.
7. **A saved workflow.** On the finished renewal-pack task, choose **Save as
   workflow**. From its page, click **Run now**.
   **Expect:**
   - it appears under Saved workflows in the rail;
   - the new run has the workflow's name;
   - the workflow page lists the run.
8. **Revoking mid-task.** Start task 1 again, and leave it at its approval. In a
   second browser profile, sign in as `it.admin@northwind-ops.com` and open
   **Admin console**. Under Mara's agent, click **Revoke** next to **Files**.
   **Expect:** the task stops at once, with the reason shown.
9. **Escape twice.** Start task 3 again. While Chromium is filling the form,
   press **Escape twice** quickly, anywhere.
   **Expect:**
   - the **Type and click for you** grant switches off;
   - the task cannot type any more.

   macOS: this needs Input Monitoring from part 1.

**Record:**
- which tasks matched their Expect line;
- for any that did not, the task's **Trajectory** tab. Its log file is in
  `agent/.ondo/runs/`: attach the `.jsonl`.

---

## 5. Desktop and screen with a real model (optional)

The scripted model can only drive the sample Linux apps. To see a task drive
Notepad or TextEdit, you need a real model:

1. Put `OPENROUTER_API_KEY` in the environment.
2. Unset `ONDO_MODEL_PROFILE`.
3. Restart `ondo-agent connect`.

Grant **See the screen while you work** for Notepad or TextEdit, and **Type
and click for you**.

Then try these:

- *In Notepad, replace the text with a one-line summary of the Halleck contract.*
  (On macOS, say TextEdit.)
  **Expect:**
  - the Trajectory tab shows `desktop_windows`, then `desktop_inspect`, then
    `desktop_act` on a named element;
  - no screenshots, because the accessibility tree was enough.
- With Notepad or TextEdit in front, open the assistant panel and use **Ask
  about this screen**: *What does this window say?*
  **Expect:** an answer about that window only.
- Try an excluded window, for example one titled "Password manager": ask about
  it.
  **Expect:** a refusal that names the policy, and nothing captured.

**Record:** the Trajectory tab of each, and the model you used.

---

## 6. Deployment

### Windows: the installer, managed policy and enrollment

Run PowerShell **as Administrator**, from the repository root.

1. **Build and install:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\windows\build.ps1
   powershell -ExecutionPolicy Bypass -File out\intune\install.ps1 -Source out\intune
   powershell -ExecutionPolicy Bypass -File out\intune\detect.ps1; $LASTEXITCODE     # expect 0
   Get-ScheduledTask -TaskPath "\Ondo\"                                                 # expect "Ondo Quartermaster"
   ```
2. **Make an enrollment token.** In the admin console (signed in as
   `it.admin@northwind-ops.com`), open **Access and policy**, then
   **Deployment: enrollment tokens**, and create one. Copy it.
3. **Set the policy as Intune would**, directly in the registry:
   ```powershell
   $k = "HKLM:\SOFTWARE\Policies\Ondo\Quartermaster"
   New-Item $k -Force | Out-Null
   Set-ItemProperty $k ControlPlaneUrl "http://localhost:8787"
   Set-ItemProperty $k EnrollmentToken "<the token>"
   Set-ItemProperty $k AllowedConnectors "ticketing;mail"
   Set-ItemProperty $k ExcludedPaths "**/Finance/Payroll/**"
   ```
4. **Check that the agent reads it.** With Notepad open, in a normal (not
   administrator) PowerShell, from `agent`:
   ```powershell
   ondo-agent platform-check --window Notepad --no-screen
   ```
   **Expect:** the `managed policy` line lists `AllowedConnectors,
   ControlPlaneUrl, ExcludedPaths`, and never the token.
5. **Enroll as the installed agent would**, as a normal user:
   ```powershell
   Rename-Item $HOME\.ondo\agent.json agent.paired.json    # keep the pairing from part 4
   $env:ONDO_USER_EMAIL = "mara.okonjo@northwind-ops.com"  # unless `whoami /upn` already prints a matching address
   & "C:\Program Files\Ondo Quartermaster\ondo-agent.exe" --config C:\ProgramData\Ondo\ondo.yaml enroll
   ```
   **Expect:** *enrolled as agent …; confirm this computer in Ondo on the web*.
6. **Confirm it.** As Mara, the home page shows *Your IT team set up Ondo on
   <this PC> for you*. Choose **Yes, it's mine**.
   **Expect:**
   - the admin console lists the agent;
   - the audit log shows `agent.enrolled` and `agent.confirmed`.

   Enroll once more and choose **Not mine**.
   **Expect:** that agent disappears, and the audit log shows `agent.rejected`.
7. **Check the installed app starts on its own.** Sign out of Windows and back
   in.
   **Expect:**
   - `Get-Process ondo-agent` shows it running as you;
   - it connects with the enrolled credentials.

   Tasks will not run on it: the installed configuration expects your
   organisation's model gateway.
8. **Clean up:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File out\intune\uninstall.ps1
   powershell -ExecutionPolicy Bypass -File out\intune\detect.ps1; $LASTEXITCODE     # expect 1
   Remove-Item "HKLM:\SOFTWARE\Policies\Ondo" -Recurse
   Rename-Item $HOME\.ondo\agent.json agent.enrolled.json; Rename-Item $HOME\.ondo\agent.paired.json agent.json
   ```

### macOS: managed policy

There is no macOS installer yet. A configuration profile from an MDM writes
managed preferences, and so does this, for testing:

```sh
sudo mkdir -p "/Library/Managed Preferences"
sudo defaults write "/Library/Managed Preferences/com.ondo.quartermaster" AllowedConnectors -array ticketing mail
sudo defaults write "/Library/Managed Preferences/com.ondo.quartermaster" DisablePixels -bool true
ondo-agent platform-check --window TextEdit --no-screen
```

**Expect:** the `managed policy` line lists `AllowedConnectors, DisablePixels`.

Then restart `ondo-agent connect` and repeat task 5 from part 4.

**Expect:** Mail can be allowed, but **Calendar is never offered**, because
this computer's policy leaves it out.

Clean up with
`sudo rm "/Library/Managed Preferences/com.ondo.quartermaster.plist"`.

---

## 7. What to send back

For each machine:

- [ ] OS and version, display scaling, one monitor or more.
- [ ] Part 2: the pytest summary line, and the output of any failure.
- [ ] Part 3: the full output of each `platform-check`, and whether the
      screenshot showed exactly the window.
- [ ] Part 4: which of tasks 1 to 9 matched their Expect line; for the others,
      the run's `.jsonl` from `agent/.ondo/runs/`.
- [ ] Part 5, if run: the model, and each Trajectory.
- [ ] Part 6: which steps matched; for the others, the command output.

Anything that fails here is a bug in code that has never run on that
platform, not a flaw in the test. Report it as it happened, with the output.
