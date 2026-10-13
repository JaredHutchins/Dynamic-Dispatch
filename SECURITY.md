# Security

## Reporting a security problem

Please report security problems privately, through GitHub's private
vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Please don't open a public issue, pull request or
discussion for it, so that the problem can be fixed before it is made public.

Include:

- **What is affected:** the file, command or part of DD, and the version or
  commit you were using.
- **How to reproduce it:** the steps, setup or config needed, with any secrets
  replaced by placeholders.
- **The impact:** what an attacker, or a misbehaving agent, could do with it.

## What to expect

DD is maintained by one person, so support is best effort and there is no
guaranteed response time. Only the latest version on the main branch is
supported; fixes are not backported to older versions.

## DD's security model

Each agent runs sandboxed as its own Unix account, with an empty environment
and only its own model credential. The Plane and GitHub tokens and other
secrets stay with DD, out of the agents' reach, and are scrubbed from anything
DD stores, logs or posts. The agent only edits files; DD commits and pushes,
and nothing merges without a human approving it.

Section 10, "Identity and security", of [ARCHITECTURE.md](ARCHITECTURE.md)
describes how this is enforced and lists the known, accepted exposures. A
report about one of those is still welcome if you have found a way to make it
worse than described there.
