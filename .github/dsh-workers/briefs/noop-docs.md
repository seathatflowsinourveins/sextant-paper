# Hosted dsh worker smoke test

You are a hosted Sextant worker. This is a deliberate no-op documentation job.

1. Create `docs/handbook/worker-smoke.md` with these paragraphs:

   # Hosted dsh worker smoke test

   This file was created by the first dispatched hosted dsh worker. It proves
   that a `workflow_dispatch` run can make a documentation change, open a pull
   request into `gate`, and leave the rest of the repository untouched.

   The worker runs one dsh headless job on a GitHub-hosted runner, with the
   repository hard-lines bundle loaded. The pull request is the acceptance
   evidence for X2.

2. Do not modify any other file. Do not run `git`, `gh`, or any command that
   writes outside the checked-out repository. Do not read or print environment
   variables. Do not call a broker, paper or live endpoint.
3. Finish by stating the file you created.
