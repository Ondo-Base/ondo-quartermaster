# Deploying to Windows with Intune

This is how an IT team puts Ondo on its Windows PCs and controls it without
talking to us. None of it has been run on a real Intune tenant yet. The pieces
that could be tested on Linux are covered by tests. What is left unverified is
listed at the end.

## What IT does

1. **In the Ondo admin console**, open **Access and policy**, then
   **Deployment: enrollment tokens**, and create a token. It is shown once.
   Under **Policy**, list the connectors people may allow.
2. **In Intune**, import the policy template: `deploy/windows/policy/OndoQuartermaster.admx`
   and `en-US/OndoQuartermaster.adml`. Use Devices, Configuration, Import ADMX.
   Then create a configuration profile from it:
   - **Control plane address**: your control plane, for example `https://ondo.example.com`.
   - **Enrollment token**: the token from step 1.
   - Any of the access settings below. They only ever narrow what is allowed.
3. **Build the app** on a Windows machine with Python 3.12:
   `powershell -File deploy\windows\build.ps1`. Wrap `out\intune` with
   Microsoft's Win32 Content Prep Tool:
   `IntuneWinAppUtil.exe -c out\intune -s install.ps1 -o out`. You can also
   run the **windows** workflow by hand and download its artifact.
4. **Add it in Intune** as a Windows app (Win32) with these settings:
   - install command: `powershell -ExecutionPolicy Bypass -File install.ps1`
     (add `-GatewayKey <key>` if your model gateway needs one);
   - uninstall command: `powershell -ExecutionPolicy Bypass -File uninstall.ps1`;
   - install behaviour: System;
   - detection: the custom script `detect.ps1`.

   Assign it to your device groups.

## What happens on each PC

- `install.ps1` does four things:
  - copies the agent to `Program Files\Ondo Quartermaster`;
  - writes its configuration to `ProgramData\Ondo`, where only administrators
    can change it;
  - optionally sets the gateway key;
  - registers a scheduled task that starts the agent in each person's session
    at sign-in, with that person's rights.
- On first start the agent finds no credentials. It reads the control plane
  address and the enrollment token from the policy, and enrolls itself for the
  person signed in. Their address is taken from `whoami /upn`, which must match
  a person in Ondo (as SCIM provisions them).
- The new agent does nothing yet: no tasks, no grants. The next time that
  person opens Ondo on the web, they see "Your IT team set up Ondo on
  NW-LT-4471 for you", with **Yes, it's mine** and **Not mine**. If they choose
  **Not mine**, the agent is switched off and the audit log records it. So a
  leaked enrollment token cannot be used to receive someone else's tasks.
- From then on it is like a paired agent. The person grants files, screen and
  input, and allows each connector the first time a task uses it.
  Administrators can revoke anything from the admin console.

## The policy settings

The agent reads these from `HKLM\Software\Policies\Ondo\Quartermaster`. On
macOS it reads the same keys from a configuration profile for
`com.ondo.quartermaster`. Where a setting takes a list, the entries are
separated by semicolons.

| Setting | Effect |
| --- | --- |
| Control plane address | Where the agent connects and enrolls. |
| Enrollment token | Used once, to enroll for the signed-in person. |
| Connectors allowed on this computer | Only these, and only if the organisation's policy allows them too. Enabled but empty means none at all. |
| Connectors turned off on this computer | Never started or offered here. |
| Access that cannot be granted | Any of `files`, `screen`, `input`. |
| Excluded folders and files | Added to the organisation's exclusions. |
| Excluded windows | Added to the organisation's exclusions. |
| Turn off screen watching | No "Ask about this screen". |
| Turn off operating windows by screenshot | Files, browser and accessibility tree only. |

Settings only narrow. The agent applies them on top of its own configuration,
and again on top of the organisation's policy each time the control plane sends
it. In `managed.narrow_policy`:
- a connector must be allowed by both the organisation's policy and device
  management;
- exclusions and disabled grants from both are added together.

The admin console shows which settings are managed on each agent, but never the
token.

## The On-device Agent Registry (ODR)

The plan says to register Ondo's capabilities with Windows' On-device Agent
Registry, and to use its built-in File Explorer and Settings connectors, so
that people can manage MCP access from Windows Settings. **That is not built.**
The ODR is prerelease, and its documentation (learn.microsoft.com) could not be
reached from the environment this was built in. Writing its package manifest,
registration file or connector ids from memory would produce something that
looks finished and is not.

What is ready for it:

- **Using the built-in connectors** is configuration, not code. A connector in
  `ondo.yaml` is any MCP server started by a command. Declare its tools'
  effects and give it the `odr.exe` command that proxies to a registered
  server, and it goes through the same consent, gates and audit as ours. The
  command syntax and server ids must come from Microsoft's current docs.
- **Registering Ondo** needs two things that do not exist yet:
  - an MSIX package with package identity, instead of the Win32 installer
    above;
  - an MCP server that exposes Ondo's own capabilities, such as starting a
    saved workflow.

## Not verified

- Every script here, and the Windows code paths in the agent, have never run on
  Windows:
  - `read_windows` in `managed.py`;
  - the `whoami /upn` detection;
  - the UIA and win32 backends, which have always been untested.

  Run the **windows** workflow first. It runs the agent tests on Windows
  (without the Linux-only desktop tests), builds the package, and installs,
  detects and uninstalls it on a GitHub-hosted runner.
- A real Intune tenant: the ADMX import, Settings Catalog ingestion, and the
  Win32 app lifecycle.
- The macOS configuration profile path.
