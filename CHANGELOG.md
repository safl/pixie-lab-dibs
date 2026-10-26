<!--
SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>

SPDX-License-Identifier: BSD-3-Clause
-->

# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `GET /agents` explains the server to whoever is pointed at it: setup
  with this server's URL filled in, what is claimed right now, and the
  agent skill's rules. The web page links to it.

## [0.2.0] - 2026-10-27

### Added

- Parts: a thing can have parts named `<thing>.<part>`, claimable on
  their own. Claiming the thing covers its parts, so it conflicts with
  another user's claim on any part, and a part conflicts with another
  user's claim on the thing. Forcing the thing takes over its parts.
- `dibs skill install` puts a Claude Code skill in
  `~/.claude/skills/dibs/` that teaches an agent to claim a lab machine
  before using it, to claim the whole machine for anything through the
  BMC, and to release when done. `dibs skill print` shows it.

## [0.1.1] - 2026-10-27

### Fixed

- `dibs server install` over a running service now restarts it, so
  installing again after an upgrade runs the new version. It used to
  write the new unit and leave the old process running.
- The system service refused every key at `dibs register` on hosts that
  resolve users without nss-systemd, plain Debian among them: under
  `DynamicUser=`, `ssh-keygen` could not look its own user up. `dibs server install` now creates a `dibs` system user and runs the service
  as it. Run `sudo dibs server install` again to update an installed
  service.

## [0.1.0] - 2026-10-27

### Added

- The first version: things to claim, fed from a config file and from
  pixie servers; claims that are all or nothing, never expire unless
  asked to, and say when an agent made them; users who register an SSH
  public key, are approved by the admin, and log in by signing a
  one-time challenge; a login hook that shows a machine's claim; and
  one `dibs` command for the server, its systemd service, the admin, and
  everyone claiming.
