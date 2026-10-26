---
name: dibs
description: Claim a shared lab machine with dibs before using it, and release it after. Use before you SSH into, run tests on, flash, reboot, power-cycle, or open the BMC or console of any lab machine (e.g. epyc-01, epyc-02, ryzen-01), including through cijoe, ipmitool or Redfish.
---

<!--
SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>

SPDX-License-Identifier: BSD-3-Clause
-->

# Claim lab machines with dibs

Lab machines are shared with people, other agents and CI. dibs says who
is on which. A claim is a note, not a lock: nothing stops you from
logging in to a machine someone else holds, so it is on you not to.

## Once per conversation

1. Check that your human has set up this workstation:

   ```
   dibs whoami
   ```

   It prints your human's name. If it fails, or `dibs` or `DIBS_URL` is
   missing, stop and tell your human to read `$DIBS_URL/agents`, or the
   dibs README. Never run `dibs register` or `dibs login` yourself: you
   claim with your human's login.

1. Get a session tag for this conversation, once, with `dibs session`.
   It prints a random tag such as `s-3f2a9c41`. Do not make one up: two
   agents could pick the same. Pass it, and who you are, on every dibs
   command, inline, so every call agrees:

   ```
   DIBS_ASSISTED_BY="Claude Code:<your model id>" DIBS_SESSION=<tag> dibs ...
   ```

   Your claims then say which agent made them, and another agent of the
   same person cannot take or release them without `--force`; nor can you
   theirs.

## Before you touch a machine

1. Look: `dibs list --json`, and read the JSON rather than the text.

1. Claim it, saying what for:

   ```
   DIBS_ASSISTED_BY=... DIBS_SESSION=... dibs claim <machine> --for "<what you are doing>"
   ```

1. Exit status 1 means someone else holds it, maybe another agent of
   your human's. Do not use the machine and do not pass `--force`. Tell
   your human who holds it (the error says) and ask what to do.

Anything that goes through the BMC or affects the whole box needs the
whole machine: power-cycling, the serial or graphical console, booting
or reflashing it, firmware, and kernel or driver changes. Claim
`<machine>` for those.

Some machines list parts in `dibs list`, named `<machine>.<part>`. Claim
a part only when your work stays inside it and cannot disturb the rest
of the machine; claiming the machine takes all of its parts. Claim
several things in one command; it is all or nothing.

## When you are done

Release what you claimed, and also when the work fails, is abandoned, or
waits on your human for long:

```
DIBS_ASSISTED_BY=... DIBS_SESSION=... dibs release --mine
```

This releases only this session's claims: not your human's, and not
other agents'.

## Never

- Use a machine claimed by someone else, or one you have not claimed.
- Pass `--force`, or release a claim this session did not make.
- Register or log in to dibs yourself.
- Power-cycle, reflash, reboot or use the BMC of a machine when you hold
  only a part of it.
