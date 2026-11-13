# Run Sandboxes on a Fleet

A Fleet pools the Sandbox capacity of several machines behind one control plane; the
contract is [the Fleet spec](../specs/fleet.md). Every machine involved runs this engine at
the same framework identity and shares one Fleet token. Put the token in each checkout's
`.env`, which every `ale` and `ale-fleet` command reads:

```bash
ALE_FLEET_TOKEN=<a long random string>
```

Connect the machines however suits you, one LAN or Tailscale: every Node and every Run's
machine must reach the control plane's port, and every Run's machine each node agent's port
([Network](../specs/fleet.md#network)). The Fleet does not encrypt, so keep these ports on a
private network.

## Start the control plane

On one always-on machine:

```bash
uv run ale-fleet serve
```

It listens on port 7470 and keeps its ledger in `~/.local/state/ale/fleet/fleet.db`
(`--db` to move it); a restart resumes from it.

## Add a Linux Node

On a machine with Docker, and KVM for VMs:

```bash
uv run ale-fleet node --control-plane http://CONTROL:7470 --address http://NODE:7471
```

`--address` is where every Run's machine reaches this agent. The control plane connects back
to it before it counts the Node and refuses one it cannot reach there, which stops the agent
with the reason; a loopback address is refused unless the control plane listens on loopback
itself. The agent lends every Provider whose preflight passes (`--provider docker --provider
qemu` names them, and then each must pass) and offers the machine's CPUs and memory less
`--reserve-cpus` and `--reserve-memory-mb`. It prepares the images it serves itself, as a
local Run would, so it pulls bases from GHCR and builds Task images with the machine's own
Docker and KVM. Its images, the build contexts Runs send and its upload scratch space live
under `$ALE_CACHE_DIR`, which belongs on local SSD for the same reason as
[VM overlays](../../packages/ale-run/src/ale/run/providers/README.md#qemu).

Stopping the agent destroys every Sandbox it holds, apart from retained ones. If the agent
crashes instead, start it again with the same options: it drives again the Sandboxes whose
Runs still hold them, keeps the retained ones and the snapshots, and destroys the rest, from
the journal it keeps in `$ALE_CACHE_DIR/fleet`. A process supervisor that restarts it
(systemd's `Restart=on-failure`, launchd's `KeepAlive`) does this unattended.

## Add a Mac Node

A Mac Node is a desktop Mac on mains power, with the [local Orchard](macos-orchard.md) of
one account. It offers two macOS VM slots (`--macos-vm-slots` lowers that when the machine
runs macOS VMs of its own).

Run `uv run scripts/setup_macos.py` once from a login, which installs the framer with
sudo, then stop it. Both the Orchard worker and the node agent then run as LaunchDaemons:
macOS's Local Network privacy exempts them, so the worker reaches its VMs without a login
session, and both start at boot. As root, write
`/Library/LaunchDaemons/org.agentslastexam.ale.orchard.plist`, with `ACCOUNT` the account and
`CHECKOUT` its ALE checkout:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.agentslastexam.ale.orchard</string>
  <key>UserName</key><string>ACCOUNT</string>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>/Users/ACCOUNT</string></dict>
  <key>WorkingDirectory</key><string>CHECKOUT</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/ACCOUNT/.local/bin/uv</string><string>run</string>
    <string>scripts/setup_macos.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/ACCOUNT/Library/Logs/ale-orchard.log</string>
  <key>StandardErrorPath</key><string>/Users/ACCOUNT/Library/Logs/ale-orchard.log</string>
</dict>
</plist>
```

and `org.agentslastexam.ale.fleet-node.plist` the same way, with its own label and log and
these arguments:

```xml
    <string>/Users/ACCOUNT/.local/bin/uv</string><string>run</string>
    <string>ale</string><string>fleet</string><string>node</string>
    <string>--control-plane</string><string>http://CONTROL:7470</string>
    <string>--address</string><string>http://NODE:7471</string>
    <string>--provider</string><string>orchard</string>
```

Then load both:

```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/org.agentslastexam.ale.orchard.plist
```

```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/org.agentslastexam.ale.fleet-node.plist
```

After an engine update that changes the framer, stop the Orchard daemon
(`sudo launchctl bootout system/org.agentslastexam.ale.orchard`) and run setup from a login
once more, since only then can sudo ask for the password.

## Run on the Fleet

Select the Fleet for the image kinds it should serve, in a run configuration or with
`--set`:

```toml
[container]
provider = "fleet"

[vm]
provider = "fleet"

[fleet]
url = "http://CONTROL:7470"
```

```bash
uv run ale run TASK --agent claude-code --set container.provider=fleet --set fleet.url=http://CONTROL:7470
```

The Run prepares no image itself: each Node prepares the images it runs, so the Run's
machine needs neither Docker nor KVM for the kinds it sends to the Fleet. It sends a Node a
Task's build context the first time a Sandbox there needs the image. Its Gateway stays on
its own machine; Nodes reach it through the tunnel the Run opens to each, so the Run's
machine accepts no connection from them. A Node the Run cannot reach is refused back to the
control plane, and the Run uses it no more.

A Run sends its plan before its first episode and prints the Nodes chosen for each image;
its first Sandboxes go there, and those Nodes prepare the image. Progress appears on the
control plane as episodes start and end.

`sandbox_retention` works as it does locally. A kept Sandbox stays on its Node and keeps
its resources reserved there. A snapshot stays on the Node that saved it, and Sandboxes that
boot from it run there. `--continue-from` continues either one, as long as the Fleet is the
Provider for its image kind. To list or destroy them, name the control plane:

```bash
ALE_FLEET_URL=http://CONTROL:7470 uv run ale sandbox list
```

## Watch the Fleet

Open `http://CONTROL:7470/?token=TOKEN` once; the page then remembers the token and
refreshes itself. From a shell:

```bash
uv run ale-fleet status --control-plane http://CONTROL:7470
```

`--json` prints the document the page is drawn from, and `ALE_FLEET_URL` saves repeating the
address. `ale-fleet drain NODE` stops new placements on a Node before maintenance, and
`ale-fleet resume NODE` ends that; `ale-fleet destroy SANDBOX` and `ale-fleet cancel RUN`
clean up after a Run that cannot.
