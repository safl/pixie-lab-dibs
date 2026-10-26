<!--
SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>

SPDX-License-Identifier: BSD-3-Clause
-->

# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- The first version: things to claim, fed from a config file and from
  pixie servers; claims that are all or nothing, never expire unless
  asked to, and say when an agent made them; users who register an SSH
  public key, are approved by the admin, and log in by signing a
  one-time challenge; a login hook that shows a machine's claim; and
  one `dibs` command for the server, its systemd service, the admin, and
  everyone claiming.
