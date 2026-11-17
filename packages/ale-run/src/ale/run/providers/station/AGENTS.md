# Station Provider: problems and decisions

A Station is a Windows Server machine its operator owns, with licensed software installed
once for every account. Each Lane is an ordinary account with a Remote Desktop session of
its own and serves one Sandbox at a time. `stationd.py` is the service that runs there
(`ALEStation`); `provider.py` is the leaf that leases its Lanes, which the Providers
[README](../README.md#station) describes; `scripts/setup_station.py` enrolls the machine;
the operator's guide is
[`docs/guides/station.md`](../../../../../../../docs/guides/station.md). This file records
the problems peculiar to Stations: what we saw, what we measured, and what we decided.

## Lanes are Remote Desktop sessions held from outside the machine

**Problem.** Several agents must use one machine's licensed software at once, each with a
desktop of its own that takes real input and gives real screenshots.

**Context.** Measured in November 2026 on Windows Server 2022 and 2025 with Remote Desktop
Session Host, one account and one session per Lane:

- 12 Lanes on 8 vCPU and 32 GB typed, opened menus and pasted concurrently with no input
  reaching another Lane, at the same speed as 4 Lanes.
- With RDP clipboard redirection on, what one Lane copied appeared in every other Lane
  and on the console.
- A keep-alive client running in the Station's console session stopped every Lane from
  taking input or giving screenshots once the console locked or disconnected; one in
  session 0 crashed. FreeRDP 3 on another machine kept every Lane working.
- A session with no client attached takes no input and gives no full-screen capture.
- Six Codex agents in laneA–laneF drove SketchUp, Rhino and Materials Studio at once
  through Cua Driver, each on its own pipe `\\.\pipe\ale-cua-driver-<lane>`; every result
  was correct and in its own Lane. Cua Driver's pipe admits only its owner
  (`D:P(A;;FA;;;<Lane SID>)`), so laneB was refused laneA's pipe.

**Solution.** Setup installs Session Host and turns off every redirection, uses the
hardware GPU for every session and lets no session time out. The operator's keep-alive box
holds each Lane's session from outside. A lease takes a Lane only while its session is
active at the size the request names. At lease time the service starts the Lane's Cua
Driver as the Lane's user on a pipe of the lease's own (below): whoever serves it sees and
drives that Lane's desktop.

**Decisions.**
- The keep-alive box is the operator's, not ALE's: it must sit outside the Station, and
  what it runs does not change what an episode measures.
- The Station never grants the agent elevation (`ALE_AGENT_SUDO=0`); a Task that needs
  `[agent].sudo` does not run on a Station.
- The Lanes' group is `ALE Lanes`, not `Lanes`: Windows account and group names share one
  case-insensitive namespace, and the 19th Lane, `laneS`, collided with a group `Lanes`.

## guestd runs as SYSTEM in the Lane's session

**Problem.** guestd does framework work as an administrator and serves the Lane's desktop,
while the agent's commands run as the Lane.

**Context.** A process as SYSTEM with the Lane's environment takes its programs, its
Python's user site and its temporary files from directories the Lane writes to: CPython on
Windows loads every `.pth` of `%APPDATA%\Python\Python312\site-packages`, so a line the
agent put there ran as SYSTEM when the framework next ran Python. Measured on the Station
in November 2026: the python.org installer's `C:\Python312`, first on the machine's PATH,
inherited `BUILTIN\Users:(CI)(AD)` and `(CI)(WD)` from `C:\`, so every Lane could add
programs and `.pth` files there for SYSTEM and the other Lanes to run.

**Solution.** The service duplicates its own SYSTEM token, moves it to the Lane's session
(`TokenSessionId`, which needs `SeTcbPrivilege`) and starts guestd with
`CreateProcessAsUserW` on `winsta0\default`, with the service's own environment, never the
Lane's, plus `ALE_AGENT_USER`, `ALE_AGENT_HOME`, `ALE_AGENT_SUDO`, the lease's Cua Driver
pipe as `ALE_CUA_DRIVER_SOCKET`, and `TEMP` and `TMP` at `C:\ProgramData\ALE\temp\<lane>`,
which no Lane enters. guestd runs the agent's commands with the Lane's token and the
environment Windows gives the Lane (`guestd/windows.py`). The framework runs its own Python
isolated (`-I`) and Windows PowerShell by its full path. Setup closes `C:\Python312` to
Lanes, and the check requires every directory of the machine's PATH closed to them. The
service starts several processes at once, each with inheritable pipe ends, so each start
names its handles with `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`: otherwise one Lane's guestd
could inherit another's pipe. The Gateway tunnel (`guestd/tunnel.py`) runs as SYSTEM in
session 0. Each runs in a job that ends with the lease, or with the service: signing the
Lane out ends what runs as the Lane, but not a process guestd started as SYSTEM, such as
one a Task's setup left behind. They need a job each: a job holds the processes of one
session, and Windows refuses a process of another with access denied.

## A Lane's pipe and port are the lease's own

**Problem.** Any account may create a named pipe of a name nobody serves, or bind any
loopback address and port nobody holds, and lists both. With names fixed per Lane, one
Lane could take another's Cua Driver pipe or Gateway port before its lease, failing every
lease of it, or after its Driver or tunnel ended, and then take its desktop's input, give
it screenshots of its own, or receive its Gateway token.

**Solution.** Each lease's Cua Driver serves `\\.\pipe\ale-cua-driver-<lane>-<random>`,
which guestd is told in `ALE_CUA_DRIVER_SOCKET` and passes to the agent's commands. Before
each call, `guestd/gui.py` requires the pipe's owner to be the desktop's account, or
Administrators for an elevated Driver: the owner is the account whose process made the
pipe, which no standard account can set to another. Cua Driver lets go of a client whose
process is another account's within milliseconds, even one acting as the Lane, as guestd's
launcher does (measured), so the owner may be gone before it is read; the account of the
process serving the pipe, known as it connects, then decides. An agent's own MCP server,
which a CLI may start without that variable, finds the pipe its account owns among those
named after it. When the Driver exits the service starts it again on the same pipe, and
ends the lease after three failed starts. Each lease's tunnel listens at a loopback address
drawn at random for the lease, on a socket the service binds exclusively at the first
`tunnel` request and holds until the lease ends; each tunnel process inherits it
(`--accept`), so the port stays taken between one tunnel and the one the Provider starts
after a dropped SSH session.

**Decisions.**
- No MCP variable names the pipe: the `cua-desktop` server's configuration, and its
  digest, stay the same for every Lane.

## The Host reaches the service through an SSH forced command

**Problem.** The Host must reach a service on a machine it does not own, without a new
listening port and without an administrator's login.

**Solution.** Setup creates `ale-host`, a standard account whose keys run only
`stationd.py client` (`ForceCommand`, `restrict`, no password, no forwarding). The client
passes the request in `SSH_ORIGINAL_COMMAND` to `\\.\pipe\ale-stationd` and splices its
standard streams to it, as Daytona's Provider reaches guestd over `ssh -T`. The pipe
refuses remote clients and admits SYSTEM and `ale-host` only. Its first instance claims
the name, as the service starts and before it resets any Lane, so the service fails to
start if another process holds it, and the client connects only when the pipe's owner is
SYSTEM: a Lane that took the name while the service was down would otherwise receive the
Host's tokens. A client that leaves before it is accepted, such as an `ssh` killed
meanwhile, costs its instance and nothing else. The pipe is overlapped, because a
synchronous handle serializes one thread's read with another's write.

## Leases, liveness and generations

The lease store is `C:\ProgramData\ALE\station\state.json`, replaced in one step, and again
while another process, such as setup reading it, holds it open for a moment. A lease
lives while any of its `guestd` or `tunnel` streams is connected and for 120 s after the
last one closes, which covers a dropped SSH connection the Provider opens again. A token
names its Lane and generation and is kept only as a SHA-256; a passed check advances the
generation, so a token outlives neither its lease nor the reset that follows it, and a
RunLock can name the exact Lane state it ran on. When the service starts, a Lane that was
leased or resetting is reset again, since nothing it ran survived; a quarantined Lane stays
out of service until setup runs again. The service's periodic work handles each Lane on its
own, so a failure with one Lane neither stops the service nor ends the others' leases. A
framework push waits for no Lane to be leased or resetting, since a reset ends what its
lease ran from the framework only as it starts, and one that fails part way puts the
installed framework back.

## The reset restores a baseline without trusting the Lane

**Problem.** A Lane is reused. The next agent must find nothing the last one left, and the
last one must not be able to change what later resets restore, nor, with another Lane's
help, make the reset write or delete anything outside it.

**Context.** Every account holds *Bypass traverse checking*, so a Lane can open any file
whose access list admits it, by path or by file ID, without being able to list the
directories above it. A baseline copied with the profile's access lists would therefore
be writable by its Lane. A handle keeps the access it was opened with whatever the access
list says later, so a Lane that opened a directory of its profile to Everyone lets a second
Lane open it and, through that handle, add or rename entries in it during the first one's
reset. Measured on Windows Server 2022:

- `robocopy /MIR`, purging a directory of the destination, follows the junctions in it
  and deletes what they point to, `/XJ` or not, and with `/B` whatever its access lists
  say: the Lane could aim a junction in its profile or recycle bin at any directory.
  `/XD` keeps it out of a junction whose parent the source has; it counts that junction
  as an extra directory in its exit code all the same.
- A DACL set with `SetKernelObjectSecurity` loses its auto-inherited mark (`AI`) unless
  the descriptor also asks for it (`SE_DACL_AUTO_INHERIT_REQ`).
- Windows keeps a user's stored credentials (Credential Manager) in memory while any
  logon session of the user lives, and sessions outlive their sign-out: every Lane kept
  the logon session of its first sign-in after boot. A credential `cmdkey` stored showed
  in the next sign-in although the reset had restored the profile's credential files.
- A reset of one Lane took 25 to 31 s, most of it reading the owner of each of the 193,000
  entries of the Station's second volume, once to delete and once to check; six at once
  took 3 to 4.5 minutes each, as they share the service's one Python.

**Solution.** The baseline under `station\baselines\<lane>` holds the profile's content,
copied without its access lists (`robocopy /COPY:DAT`), so it admits SYSTEM and
Administrators only, and a record of every entry's owner, DACL, link target and named
streams. The reset, in order: ends the lease's job; disables the account; signs the
Lane out and kills every process of its SID; waits for its hive to unload; gives the
profile's own directory to SYSTEM and Administrators and moves it aside whole, to
`<profile>.ale-old`; makes the profile anew, a directory only SYSTEM and Administrators
enter, and copies the baseline into it (`robocopy /E /COPY:DAT /B`, which copies and never
purges); makes the baseline's junctions, hidden and system files as Windows makes them;
sets every entry's recorded owner and DACL; deletes the old profile; recreates the stage
root; empties the Lane's recycle bin on every volume; deletes the Lane's scheduled tasks;
and deletes what the Lane owns in the system drive's root and recycle bin, `Windows\Temp`,
`ProgramData`, the public profile and every other fixed volume. The new profile is one only
the reset has opened, so nothing the Lane or another Lane left can redirect what robocopy
writes. Every deletion walks the tree itself, never with robocopy, and opens each entry, a
link rather than its target, by handle; it goes ahead only if the handle's own path
(`GetFinalPathNameByHandleW`) is the one meant, so a directory swapped for a junction on the
way makes the reset fail rather than delete what the junction points to. The check then
requires no process of the Lane, no file robocopy would copy back, a profile whose entries
match the record, an empty stage root admitting only SYSTEM and Administrators, no
scheduled task, nothing owned in those places, every deny of the isolation recipe, and no
directory of the machine's PATH that Lanes may write in. A Lane that passes gets its
account back and the next generation; one that fails is quarantined with the reason and
its account stays disabled. Before every lease, impersonating the Lane's session, the
service deletes each stored credential written after the baseline was taken.

**Decisions.**
- The profile is made anew rather than locked by its access lists: no access list takes
  back a handle another Lane already holds.
- A Lane whose old profile cannot be moved aside within a minute, or deleted, is
  quarantined: two Lanes working together can take one of them out of service, but cannot
  make its reset write or delete outside it.
- A Lane without a baseline is quarantined but keeps its account, so that the keep-alive
  box can sign it in and setup can take its baseline.
- Damage the reset repairs, such as a changed access list in the profile, does not
  quarantine; damage it does not repair, such as a removed deny of the recipe, does.
- Not covered: registry keys outside the Lane's hive that standard accounts may write,
  places outside the scanned ones that grant Lanes write access, and the network: Lanes
  share the machine's network stack and loopback, so a Station serves `open` Tasks only.

## The Provider waits for a Lane, then gives up

**Problem.** A Run with more Sandboxes at once than the Station has free Lanes, or one
whose Lanes are resetting, would fail its episodes as soon as none is free.

**Solution.** `ale-stationd` runs the probes of a `lease` request, within 120 s in all,
and holds the request up to 60 s for a Lane to come free; the Provider waits for its reply
as long as both may take and a minute more. While it answers `busy`, the Provider asks
again for up to `sandbox.station.lane_wait_sec`, 1800 s by default, and then fails the
Sandbox with `ProviderCapacityError`, an environment error that `episode_retries` retries.

**Decisions.**
- The wait is a Run setting, not the Station's: what is worth waiting for depends on the
  Run, and a Station serves several.
- CPUs and memory are recorded as the Task asked and not admitted against the Station's:
  several Runs share a Station, and only `ale-stationd` could count them all.

## A Lane's files are its own

**Problem.** QEMU's Windows upload access lists grant Users read and execute. On a Station
every other Lane is in Users and holds *Bypass traverse checking*, so it could open such a
file by its path below a directory it cannot list.

**Solution.** The Provider resets every upload, and every file it writes for the agent, to
an access list that grants SYSTEM, Administrators and the identity's own account only
(`privileges.reset_windows_acl(private=True)`). The Lane's stage root admits SYSTEM and
Administrators alone, as `/opt/ale` admits root: a Task's stages are the framework's, and
none of them is for the agent to read.

## Tokens travel in SSH command lines

A request is the SSH session's command, so a lease's token is in the command line of the
Host's `ssh` processes, which every account on the Host can list. It is worth nothing
without the key that signs in as `ale-host`, which only the operator of the Run holds; a
Host shared with accounts that hold that key shares the Station with them anyway.
