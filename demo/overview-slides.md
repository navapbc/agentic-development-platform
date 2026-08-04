---
marp: true
paginate: false
---

# Platform Overview

Turn a tracked ticket into a reviewed pull request, automatically and safely.

- Picks up a work item from Jira, the team's issue tracker
- Studies the target codebase to find where the change belongs
- Has an AI coding agent write a small, focused change
- Runs the change through review and policy checks
- Opens a pull request for a person to approve

It behaves like a careful junior engineer. It never loses work to a crash, never does the same thing twice, and always leaves a trail you can audit.

---

## What the Platform Already Guarantees

- **Durability.** If the process running a task crashes, the run resumes from its last saved step instead of starting over.
- **Grounded context.** The AI works from a structural map of the codebase, so it changes the right files and traces what each change affects before making it.
- **Safe concurrency.** Many agents run at once without colliding, claiming the same work twice, or posting duplicate updates.
- **Auditable delivery.** Every outside action happens once and in the right order, with a full record you can trace afterward.

One deployment brings these same guarantees to every repository a team owns.
