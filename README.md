<!--
SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>

SPDX-License-Identifier: BSD-3-Clause
-->

# dibs

[![test](https://github.com/safl/pixie-lab-dibs/actions/workflows/test.yml/badge.svg)](https://github.com/safl/pixie-lab-dibs/actions/workflows/test.yml)
[![format](https://github.com/safl/pixie-lab-dibs/actions/workflows/format.yml/badge.svg)](https://github.com/safl/pixie-lab-dibs/actions/workflows/format.yml)
[![PyPI](https://img.shields.io/pypi/v/pixie-lab-dibs)](https://pypi.org/project/pixie-lab-dibs/)
[![Python](https://img.shields.io/pypi/pyversions/pixie-lab-dibs)](https://pypi.org/project/pixie-lab-dibs/)
[![License: BSD-3-Clause](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)
[![REUSE status](https://api.reuse.software/badge/github.com/safl/pixie-lab-dibs)](https://api.reuse.software/info/github.com/safl/pixie-lab-dibs)

Call dibs on shared lab machines.

A lab machine is shared with people, ai-agents and automation-runners.
dibs lets each of them see who is on a machine before touching it, by
claiming it. A claim is a note, not a lock: SSH and the console stay
open to everyone. Everywhere a claim shows up, it reads as one sentence:

```
'alice' has claimed 'node-01' for 'xnvme' expires in: 'never'
```

dibs keeps a list of **things** to claim, mostly machines. It fills the
list from **sources**: a config file, and any number of
[pixie](https://github.com/safl/pixie) servers, whose machines show up
on their own, with their hardware inventory.

## Install

```
uv tool install pixie-lab-dibs
```

That gives you the `dibs` command, for both the server and the client.
Signing in uses `ssh-keygen` from OpenSSH 8.1 or newer.

## Run the server

On the host that will run it, list the things in `/etc/dibs/dibs.toml`:

```toml
[[pixie]]
url = "http://pixie:8080"   # every machine pixie knows becomes a thing

[[thing]]
name = "ryzen-01"           # a machine pixie does not boot
mac = "aa:bb:cc:dd:ee:ff"   # any other key is kept as an attribute
```

Then make it a service and start it:

```
sudo dibs server install
```

The command writes a systemd unit for the `dibs` you ran, enables it
and starts it. `dibs server start`, `stop`, `restart`, `status` and
`logs` manage it from then on. The service runs as a `dibs` system
user, which the install creates, with write access only to
`/var/lib/dibs`. Without root, `dibs server install --user` installs a per-user service instead. For a quick try,
`dibs serve` runs the server in the foreground.

The system service can only run a `dibs` installed outside your home
directory, for example with
`sudo UV_TOOL_DIR=/opt/uv UV_TOOL_BIN_DIR=/usr/local/bin uv tool install pixie-lab-dibs`.

## Sign in

Claiming needs a user. Register a name with your SSH public key:

```
dibs --url http://dibs:8090 register alice
```

The admin approves it on the server host:

```
sudo dibs admin approve alice
```

Then you log in once. `dibs` signs a one-time challenge with your key,
through your ssh-agent if the key lives there, and keeps the token it
gets back:

```
dibs login
```

dibs remembers the server you registered or logged in to, so no later
command needs the URL; `--url` or `DIBS_URL` still pick another. The
token does not expire; `dibs logout` drops it, and
`dibs admin revoke alice` ends every login alice has. `-i` picks a key
other than `~/.ssh/id_ed25519.pub`.

Over plain HTTP, a token can be read off the network. dibs gives claims
an identity among people who share a lab in good faith; it is not
protection against someone on that network. Put HTTPS in front of it if
the network is not trusted.

## Claim

```
dibs list [--mine]
dibs info <thing>
dibs claim <thing>... --for <purpose> [--expires 2h] [--force]
dibs release <thing>... | --mine
```

A claim never expires unless given `--expires`, and an expired claim
counts as free. Claiming several things is all or nothing: if any of
them is claimed by someone else, you get none, and the refusal says who
holds it. `--force` takes or releases someone else's claim, and the
server's log records that it did.

`list` and `info` print for people. Add `--json`, `--yaml` or `--toml`
to get the answer as data instead; an agent or script should use one of
those rather than parse the text.

The exit status is 0 when done, 1 when refused because someone else
holds a thing, and 2 for anything else.

### Parts

A thing can have parts that people use independently, named
`<thing>.<part>` and listed in the config like any other thing:

```toml
[[thing]]
name = "epyc-02.gpu1"
```

Claiming `epyc-02` takes the whole machine, parts included, so it is
refused while someone else holds a part. Claiming `epyc-02.gpu1` leaves
the other parts to others, and is refused while someone else holds the
whole machine. A part listed without its thing still makes the thing
claimable.

Only make something a part if using it cannot disturb the rest of the
machine. Anything that can, such as the BMC, power, the console or
reflashing, needs a claim on the whole machine, so it is an attribute of
the thing (`bmc = "10.20.30.82"`) rather than a part.

### AI-agents

Every dibs server explains itself at `/agents`: how to set up for this
very server, with its URL filled in, what is claimed right now, and the
rules below. So the one thing to tell a person or an agent about a lab
is "before touching its machines, read `http://dibs:8090/agents`", in a
prompt, a project's `AGENTS.md`, or a message.

For Claude Code, install the skill that teaches an agent all of this,
and it applies whenever the agent is about to use a lab machine:

```
dibs skill install
```

`dibs skill print` shows it, to adapt for another agent.

An agent never registers or logs in. It claims with its human's login,
names itself the way an `Assisted-by:` trailer does, and tags its
session with a tag from `dibs session`, so that two agents of one person
do not take or release each other's claims. The login is shared; the
tag is not, because each agent passes its own on its commands:

```
DIBS_ASSISTED_BY="Claude Code:claude-opus-5-5" DIBS_SESSION=s-3f2a9c41 dibs claim epyc-02 --for "ring test"
```

The claim then reads `'alice' assisted by 'Claude Code:claude-opus-5-5' (session s-3f2a9c41) has claimed ...`. Another session cannot claim
over it or release it without `--force`. The human, claiming by hand,
can release any of their agents' claims, but needs `--force` to claim
over one.

If you are an agent reading this: run `dibs whoami` first; if it fails,
stop and tell your human, never register yourself. Before you run
anything on a lab machine, run `dibs list` and claim the machine.
Power-cycling, the console, reflashing or anything else through the BMC
needs the whole machine, never just a part. Never use a machine someone
else has claimed, and never pass `--force`; ask your human instead.
Release with `dibs release --mine` when the work is done, and also when
it fails or is abandoned. That releases only your session's claims.

### Automation-runners

Give the runner a user of its own: register it with a key made for it,
approve it, log in once, and store the token as the runner's
`DIBS_TOKEN` secret. A refused claim exits 1, which a runner can wait
out; anything else exits 2 and should fail the job. In GitHub Actions:

```yaml
env:
  DIBS_URL: http://dibs:8090
  DIBS_TOKEN: ${{ secrets.DIBS_TOKEN }}
steps:
  - name: Claim the machine
    run: |
      until dibs claim node-01 --for "${{ github.workflow }}" --expires 3h; do
        [ $? -eq 1 ] || exit 1
        sleep 60
      done
  - name: Test
    run: ...
  - name: Release the machine
    if: always()
    run: dibs release --mine
```

The `--expires` is a safety net for a runner that dies before its
release step.

## See the claim when you log in

Install the login hook on each machine. At every interactive login it
asks dibs for the machine's claim and prints it. It stays silent when
dibs is unreachable, and does nothing for non-interactive SSH such as
`scp`. It finds the machine by its MAC addresses, or else its hostname.

```
curl -fsS http://dibs:8090/login.sh | sudo tee /etc/profile.d/dibs.sh
```

## Shell completion

```
eval "$(dibs completion bash)"
```

Add that line to `~/.bashrc`, or write the script to
`/etc/bash_completion.d/dibs`. It completes commands and options, and
thing names from the server.

## HTTP

Anything that can send JSON can use dibs without the client. Reads need
no login; claim and release take `Authorization: Bearer <token>`.

| Route                      |                                                                                              |
| -------------------------- | -------------------------------------------------------------------------------------------- |
| `GET /things`              | every thing, with its `sentence` and, while claimed, its `claim`                             |
| `GET /things/<name>`       | one thing, plus `pixie` when a pixie server knows it                                         |
| `GET /things/<name>/claim` | the sentence as one line of text                                                             |
| `POST /claims`             | `{"things": [...], "purpose": "...", "assisted_by": "", "expires_in": null, "force": false}` |
| `POST /claims/release`     | `{"things": [...], "force": false}`, or `{"mine": true, "assisted_by": ""}`                  |
| `POST /auth/register`      | `{"user": "...", "pubkey": "ssh-ed25519 ..."}`                                               |
| `POST /auth/challenge`     | `{"user": "..."}` gives a nonce and the message to sign                                      |
| `POST /auth/login`         | `{"user", "nonce", "signature"}` gives a token                                               |
| `GET /agents`              | how to use this server, for a person or an agent, as text                                    |
| `GET /login.sh`            | the login hook                                                                               |

A claim or release blocked by someone else's claim answers 409 with
the blocking claims' sentences in `detail.claimed`.

## Releasing

Set the version in `src/dibs/__init__.py`, move the changelog's
`Unreleased` entries under that version, commit, and push a tag named
after it:

```
git tag v0.1.0 && git push origin v0.1.0
```

The tag runs the publish workflow, which checks the tag matches the
version, runs the checks, and uploads `pixie-lab-dibs` to PyPI through trusted
publishing.

## License

BSD-3-Clause. The repository follows [REUSE](https://reuse.software):
every file states its copyright and license, `LICENSES/` holds the
license text, and `REUSE.toml` covers the files that cannot carry a
header.
