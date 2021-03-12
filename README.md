NOTE (2021):
============

This project is only very moderately maintained and mostly dormant (though not
obsolete -- it's still used daily). It was originally developed for use early in
the Rust project's life, and has been superceded by multiple enhanced rewrites:

  - Homu:  https://github.com/barosl/homu
  - Bors-NG: https://bors.tech/ / https://github.com/bors-ng/bors-ng

I will periodically accept PRs and such for minor compatibility issues or
blockers to keep it running, but in general future feature-work or development
of the lineage should go to one of the successor projects.


Bors is an automated integrator for github and [buildbot](https://buildbot.net/).
===========

 It's written for the [rust project](http://www.rust-lang.org/), so probably contains a
 number of peculiarities of this project. You may need to do
 some work to reuse it elsewhere.

 We assume bors is run in a loop, perhaps once per minute from cron
 (github has a rate-limited API). Each time it runs it reloads its
 entire state from github and buildbot, decides what the most
 fruitful next-thing it can do is, does that one thing, and __exits__.
 This is a crude design but it means the script and workspace is
 mostly stateless and should (once debugged) never require operator
 intervention in the workspace driving it, only sometimes on the
 sites it reads from (github and buildbot).

 It requires a config file, `bors.cfg`, in its workspace.
 This config file should hold the a json dictionary:

```
 {
       "owner": "<github-username-the-owner-of-repo>",
       "repo": "<short-github-repo-name>",
       "reviewers": ["<user1>", "<user2>", ...],
       "builders": ["<buildbot-builder1>", "<buildbot-builder2>", ...],
       "test_ref": "<git-ref-for-testing>",
       "master_ref": "<git-ref-for-integration>",
       "nbuilds": <number-of-buildbot-builds-history-to-look-at>,
       "buildbot": "<buildbot-url>",
       "gh_user": "<github-user-to-run-as>",
       "gh_pass": "<password-for-that-user>"
 }
```

 For example, the rust config at the time of writing (minus password) is:
 
```
 {
       "owner": "mozilla",
       "repo": "rust",
       "reviewers": ["brson", "catamorphism", "graydon", "nikomatsakis", "pcwalton"],
       "builders": ["auto-linux", "auto-win", "auto-bsd", "auto-mac"],
       "test_ref": "auto",
       "master_ref": "incoming",
       "nbuilds": 5,
       "buildbot": "http://buildbot.rust-lang.org",
       "gh_user": "bors",
       "gh_pass": "..."
 }
```

 The general cycle of bors' operation is as follows:

 - load all pull reqs
 - load all statuses and comments
 - sort them by the `STATE_*` values below
 - pick the ripest (latest-state) one and try to advance it, meaning:

   - if `state==UNREVIEWED` or `DISCUSSING`, look for `r+` or `r-`:
     - if `r+`, set APPROVED
     - if `r-`, set DISAPPROVED
     - (if nothing is said, exit; nothing to do!)

   - if `state==APPROVED`, merge pull.sha + master => test_ref:
     - if merge ok, set `PENDING`
     - if merge fail, set `ERROR` (pull req bitrotted)

   - if `state==PENDING`, look at buildbot for test results:
     - if failed, set `FAILED`
     - if passed, set `TESTED`
	  - (if no test status, exit; waiting for results)

   - if `state==TESTED`, fast-forward master to test_ref
     - if ffwd works, close pull req
     - if ffwd fails, set `ERROR` (someone moved master on us)

License
=======

 Copyright 2013 Mozilla Foundation.

 Licensed under the Apache License, [Version 2.0](
 http://www.apache.org/licenses/LICENSE-2.0) or the [MIT license](
 http://opensource.org/licenses/MIT), at your
 option. These files may not be copied, modified, or distributed
 except according to those terms.
