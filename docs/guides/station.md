# Windows Stations

A Station is a Windows Server machine you own, with licensed software installed, whose
Lanes run Windows Tasks that need that software. Each Lane is an ordinary account with a
Remote Desktop session of its own, and serves one Sandbox at a time; after each one, the
Station resets the Lane to a baseline and checks it before the next. The Station's service,
`ale-stationd`, runs there as `ALEStation`; the Host reaches it over SSH. How it leases,
resets and checks Lanes is in the Provider's
[notes](../../packages/ale-run/src/ale/run/providers/station/AGENTS.md).

## What you prepare

- **The Station**: Windows Server 2022 or later on x86-64, reachable from the Host over
  SSH, with internet access while setup runs. Several Remote Desktop sessions at once need
  Remote Desktop licenses for the Lanes; without a license server, Windows allows them for
  a 120-day grace period. A GPU that renders, such as an NVIDIA vWS one, serves every
  session.
- **OpenSSH Server** running on it, and an administrator account the Host signs in to
  with a key. Setup runs as that account.
- **The licensed software**, installed for all users. Software that keeps its license or
  sign-in in each account's profile needs each Lane signed in once, and then a new
  baseline ([Lanes after enrollment](#lanes-after-enrollment)). Every reset deletes what
  its Lane owns in `ProgramData`, so software a Lane account first started before
  enrollment loses the shared files it made there, such as SketchUp's materials; give
  them to Administrators first (`icacls <folder> /setowner *S-1-5-32-544 /T`).
- **A keep-alive box**: a Linux machine with FreeRDP 3 and Xvfb that reaches the Station's
  Remote Desktop port ([keep-alive box](#the-keep-alive-box)). It must not be the Station
  itself: a client in the Station's console stops every Lane once the console locks or
  disconnects.
- **The Host's SSH key**, which signs in as `ale-host`, the account that may only talk to
  `ale-stationd`.

## Enroll the Station

Write the Station's settings on the Host, for example as `station.toml`:

```toml
lanes = 6                       # laneA ... laneF
resolution = "1600x900"         # the size the keep-alive box connects each Lane at
host_keys = ["ssh-ed25519 AAAA... ale@host"]
lane_password_file = "~/.config/ale/station-lanes.password"

[software.materials-studio]
probe = 'tasklist /fi "imagename eq lmgrd.exe" | find "lmgrd.exe"'

[software.notepad]
```

The password file holds the Lanes' password on one line; the keep-alive box needs it too.
It must satisfy the Station's password policy. Then, from the ALE checkout:

```bash
uv run scripts/setup_station.py admin@station.example station.toml
```

The first run installs Remote Desktop Session Host and asks for a restart; restart the
Station and run setup again. Once it finishes, start the keep-alive box for the Lanes it
names, wait until each Lane has signed in, and run setup a third time: a Lane's baseline
is its profile as it is after its first sign-in, so setup takes it only once the Lane has
signed in. Setup reports each Lane. Check the Station from the Host:

```bash
ssh -T ale-host@station.example hello
```

The reply is one JSON line with the Station's framework, manifest and Lanes. A Lane is
`idle` once its baseline exists and its reset passed.

Setup changes the Station as follows, and only where it differs, so running it again is
how you update `ale-stationd`, change the manifest or the password, add Lanes or take a
baseline:

- Remote Desktop Session Host, with no clipboard, drive, printer or device redirection
  (the clipboard was measured to leak between sessions), the hardware GPU for every
  session, and no session time limits.
- What agents expect on Windows, at the Windows base image's versions: an all-users
  Python 3.12 on the PATH from python.org's installer, in `C:\Python312`, which setup closes
  to every account but Administrators, and Node.js with npm and Git with Git Bash through
  Chocolatey.
- The Lane accounts `laneA`, `laneB`, ... in the groups `ALE Lanes` and Remote Desktop
  Users.
- `C:\ProgramData\ALE`, which SYSTEM and Administrators own: the service on its own Python
  in `station\`, which `ale-host` may also run, as its SSH keys' forced command does; Cua
  Driver, which every account may run; and each Lane's stage root in `lanes\`, where ALE
  stages a Task's files for that Lane, out of its reach.
- The account `ale-host`, with no password anyone knows, and a block in `sshd_config`
  that lets its keys run `ale-stationd`'s client and nothing else.
- The isolation recipe: Lanes may not list `C:\Users`, create entries in the root of
  `C:\`, of `C:\ProgramData` or of any other fixed volume, or write in `C:\Users\Public`;
  another fixed volume that every signed-in account may write to becomes read-only to
  them. Setup fails, and every reset's check with it, while Lanes may write in a directory
  of the machine's PATH, where what runs as SYSTEM finds its programs; close such a
  directory to them (`icacls <directory> /inheritance:r` and grants for SYSTEM,
  Administrators and Users' read and execute).
- The service `ALEStation`, started at boot and restarted after a failure.

Setup stops the service while it runs and refuses while a Lane is leased.

## The keep-alive box

Each Lane's session must stay connected: a session with no client takes no input and gives
no screenshots. Run one FreeRDP client per Lane on the keep-alive box, in a loop, since the
Station signs a Lane out at every reset and disables it until the reset passes:

```bash
#!/usr/bin/env bash
# keep-lanes.sh STATION LANE...: hold each Lane's session; password in ~/lanes.password
station=$1; shift
pgrep -f "Xvfb :1" >/dev/null || { Xvfb :1 -screen 0 3840x2160x24 & sleep 2; }
for lane in "$@"; do
    while true; do
        DISPLAY=:1 xfreerdp3 /v:"$station" /u:"$lane" /p:"$(cat ~/lanes.password)" \
            /cert:ignore /size:1600x900 -clipboard /gfx \
            +auto-reconnect /auto-reconnect-max-retries:1000
        sleep 5
    done >"$HOME/rdp-$lane.log" 2>&1 &
done
wait
```

Use the `resolution` of `station.toml` as `/size`: a lease asks for a size, and only a
Lane whose session has it is handed out. Allow the Remote Desktop port from the keep-alive
box only.

## The software manifest

`[software.<id>]` names software the Station offers, as a Task names it. An entry may give
a `probe`: a `cmd.exe` command line the service runs as SYSTEM before every lease that
names this software, which must exit 0. A failing probe refuses the lease, which the run
reports as an infrastructure error rather than the agent's failure; use it for what can
break between leases, such as a license server.

## Run Tasks on it

A Task that names software runs its solver in a Lane through the `station` Provider, and its
verifier elsewhere, such as in a container. Point a Run at the Station's `ale-host` account,
at the size the keep-alive box connects at, for example in `run.toml`:

```toml
desktop_resolution = "1600x900"         # the Station's resolution

[sandbox]
providers = ["station", "docker:runc"]  # the solver in a Lane, its verifier in a container

[sandbox.station]
destination = "ale-host@station.example"
lane_wait_sec = 1800                    # how long a Sandbox waits for a free Lane
```

Before the first episode the Run checks that the Station runs the `ale-stationd` of the ALE
checkout it runs from, so run setup again after updating ALE, and installs that checkout's
guestd, Python and Cua Driver on the Station when they differ, which the Station allows
only while no Lane is leased, waiting while one resets. A Station whose manifest has
`notepad` runs ALE's own check of a Lane end to end:

```bash
uv run ale validate tests/fixtures/tasks/station-notepad --config run.toml
```

A Lane's network is the Station's, so the Station runs only Tasks whose agent may reach
anything (`network_mode = "public"`), and it grants no agent elevation.

## Lanes after enrollment

`ssh -T ale-host@station.example status` shows each Lane: `idle`, `leased`, `resetting`
or `quarantined`, its generation, and why it is quarantined. A Lane whose reset or check
failed is quarantined with its account disabled, and its keep-alive client cannot sign in;
a Lane without a baseline is quarantined but keeps its account, so that it can sign in and
setup can take its baseline. Fix the cause and run setup again: it returns quarantined
Lanes to the reset, and they come back once it passes.

To take a new baseline of a Lane, for example after signing it in to licensed software:
sign in as the Lane through Remote Desktop, make the change, sign out, delete
`C:\ProgramData\ALE\station\baselines\<lane>` on the Station, and run setup again. The
baseline is then the Lane's profile as you left it.

A reset makes the Lane's profile anew from its baseline, empties its stage root and removes
what the Lane created in the shared places it can write to. It does not undo changes
outside them, such as registry keys outside the Lane's own hive that every account may
write, so install software that grants such access with care.
