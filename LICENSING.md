# Licensing

`deploy-barometer` is **dual-licensed**. You may use it under either of the
following, at your option:

1. The **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).
2. A **commercial licence** obtained from the copyright holder.

Copyright © 2026 crafted-glitches. All rights reserved.

---

## Which one applies to you?

### You are just running it

Run it, at home or at work, as much as you like. Running the software places no
obligation on you at all — the AGPL's conditions are triggered by *distributing*
or *offering it as a network service*, not by using it.

### You want to build on it — fork, spin-off, or reuse the source

Then the AGPL applies, and it asks something substantial in return: **your
project must also be released under the AGPL, with complete source available.**

Concretely, if you distribute a modified version, or let anyone interact with it
over a network, you must:

- release the **complete corresponding source** of your version, including your
  modifications;
- license that whole work under the **AGPL-3.0**;
- preserve the copyright and licence notices;
- state what you changed.

The network clause is the part that distinguishes the AGPL from the ordinary
GPL. Running a modified version as a service that other people use counts as
distribution, so "we only ever host it, we never ship it" does not avoid the
obligation.

### You cannot accept those terms

If you want to build on this source **without** releasing your own work under
the AGPL — a closed-source product, a proprietary fork, or anything you would
rather not publish — you need a commercial licence.

That is granted case by case, at the copyright holder's discretion.

> **To request one, contact:** `<add your preferred contact address here>`

Please describe what you intend to build and how you intend to distribute it.

---

## Contributing

Because this project is dual-licensed, the copyright holder must be able to
license the **entire** codebase commercially. That is only possible while they
hold, or have been granted, the rights to all of it.

Contributions are therefore accepted only with a **Contributor Licence
Agreement** or an equivalent copyright assignment. Without one, a merged pull
request would leave part of the codebase un-licensable commercially and would
quietly break the dual-licensing model for everyone.

If you would rather not sign one, please open an issue describing the change
instead — a described idea carries no copyright and can be implemented freely.

---

## Third-party dependencies

This project's own licence does not override its dependencies'. Almost all are
permissive (MIT or BSD) and impose no meaningful conditions:

| Dependency | Licence |
| --- | --- |
| busylib, fastapi, pydantic, pydantic-settings | MIT |
| uvicorn, httpx, protobuf | BSD |
| pillow *(calibration extra only)* | MIT-CMU |
| **zeroconf** | **LGPL-2.1-or-later** |

`zeroconf` is the one worth understanding.

**Under the AGPL path** there is no issue. Its "or-later" clause permits use
under LGPL-3.0, which is compatible with AGPL-3.0.

**Under a commercial path** it still works, but with a condition: the LGPL
allows use in proprietary software provided the library stays *replaceable* by
the end user. Installed normally with `pip`, it is — it remains a separate,
swappable package. Vendoring it into a single frozen binary, or patching it in
place, would forfeit that and pull LGPL obligations onto the surrounding work.
Distributing the Docker image counts as conveying the library, so the image
should keep it as a normal installed package, which it does.

If mDNS is unwanted, `BAROMETER_MDNS_ENABLED=false` disables it — but the
dependency is still installed, so removing it from `pyproject.toml` is the only
way to be free of it entirely.

---

## A note on GitHub forks

This repository is public, and GitHub's Terms of Service (§D.5) state that by
publishing a repository you allow other GitHub users to view and fork it. The
in-platform fork button therefore works regardless of what this file says.

That grants no rights beyond GitHub itself. What anyone may lawfully *do* with a
fork — modify it, publish it, ship it in a product — is governed entirely by the
AGPL, or by a commercial licence.
