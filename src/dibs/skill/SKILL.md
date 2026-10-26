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

## Before you touch a machine

1. Look: `dibs list`. Use `dibs list --json` to read it, not the text.
2. Claim it, naming yourself as an agent and saying what for:

   ```
   DIBS_ASSISTED_BY="Claude Code:<your model id>" dibs claim <machine> --for "<what you are doing>"
   ```

   Set `DIBS_ASSISTED_BY` on every dibs command, inline, so each one
   agrees on who you are. It uses your human's login; you need no login
   of your own.

3. Exit status 1 means someone else holds it. Do not use the machine and
   do not pass `--force`. Tell your human who holds it (the error says)
   and ask what to do.

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
DIBS_ASSISTED_BY="Claude Code:<your model id>" dibs release --mine
```

This releases only what you claimed, not your human's own claims.

## Never

- Use a machine claimed by someone else, or one you have not claimed.
- Pass `--force`, or release a claim you did not make.
- Power-cycle, reflash, reboot or use the BMC of a machine when you hold
  only a part of it.

If `dibs` is not installed or `DIBS_URL` is not set, stop and tell your
human rather than going ahead without a claim.
