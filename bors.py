#!/usr/bin/env python
#
# Copyright 2013 Mozilla Foundation.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.
#
# Bors is an automated integrator for github and buildbot
# (and travis, github-actions, and others).
#
# It's written for the rust project, so probably contains a
# number of peculiarities of this project. You may need to do
# some work to reuse it elsewhere.
#
# We assume bors is run in a loop, perhaps once per minute from cron
# (github has a rate-limited API). Each time it runs it reloads its
# entire state from github and buildbot, decides what the most
# fruitful next-thing it can do is, does that one thing, and exits.
# This is a crude design but it means the script and workspace is
# mostly stateless and should (once debugged) never require operator
# intervention in the workspace driving it, only sometimes on the
# sites it reads from (github and buildbot).
#
# It requires a config file, bors.cfg, in its workspace.
# This config file should hold the a json dictionary:
#
# {
#       "owner": "<github-username-the-owner-of-repo>",
#       "repo": "<short-github-repo-name>",
#       "reviewers": ["<user1>", "<user2>", ...],
#       "builders": ["<buildbot-builder1>", "<buildbot-builder2>", ...],
#       "test_ref": "<git-ref-for-testing>",
#       "nbuilds": <number-of-buildbot-builds-history-to-look-at>,
#       "buildbot": "<buildbot-url>",
#       "gh_user": "<github-user-to-run-as>",
#       "gh_pass": "<password-for-that-user>"
# }
#
# For example, the rust config at the time of writing (minus password) is:
#
# {
#       "owner": "mozilla",
#       "repo": "rust",
#       "reviewers": ["brson", "catamorphism", "graydon", "nikomatsakis", "pcwalton"],
#       "builders": ["auto-linux", "auto-win", "auto-bsd", "auto-mac"],
#       "test_ref": "auto",
#       "nbuilds": 5,
#       "buildbot": "http://buildbot.rust-lang.org",
#       "gh_user": "bors",
#       "gh_pass": "..."
# }
#
#
# The general cycle of bors' operation is as follows:
#
# - load all pull reqs
# - load all statuses and comments
# - sort them by the STATE_* values below
# - pick the ripest (latest-state) one and try to advance it, meaning:
#
#   - if state==UNREVIEWED or DISCUSSING, look for r+ or r-:
#     if r+, set APPROVED
#     if r-, set DISAPPROVED
#     (if nothing is said, exit; nothing to do!)
#
#   - if state==APPROVED, merge pull.sha + target-branch => test_ref:
#     - if merge ok, set PENDING
#     - if merge fail, set ERROR (pull req bitrotted)
#
#   - if state==PENDING, look at buildbot for test results:
#     - if failed, set FAILED
#     - if passed, set TESTED
#     (if no test status, exit; waiting for results)
#
#   - if state==TESTED, fast-forward target-branch to test_ref
#     - if ffwd works, close pull req
#     - if ffwd fails, set ERROR (someone moved target-branch on us)

import argparse
import json
import urllib.request, urllib.error, urllib.parse
import re
import logging
import logging.handlers
import github
from time import strftime, gmtime

__version__ = '1.2'

TIMEOUT=60

BUILDBOT_STATUS_SUCCESS = 0
BUILDBOT_STATUS_WARNINGS = 1
BUILDBOT_STATUS_FAILURE = 2
BUILDBOT_STATUS_SKIPPED = 3
BUILDBOT_STATUS_EXCEPTION = 4
BUILDBOT_STATUS_RETRY = 5

def build_has_status(b, s):
    return "results" in b and b["results"] == s

STATE_BAD = -2
STATE_STALE = -1
STATE_DISCUSSING = 0
STATE_UNREVIEWED = 1
STATE_APPROVED = 2
STATE_PENDING = 3
STATE_TESTED = 4
STATE_CLOSED = 5

def state_name(n):
    assert STATE_BAD <= n
    assert n <= STATE_CLOSED
    return [ "BAD",
             "STALE",
             "DISCUSSING",
             "UNREVIEWED",
             "APPROVED",
             "PENDING",
             "TESTED",
             "CLOSED" ][n+2]

class BuildBot:
    def __init__(self, cfg):
        self.log = logging.getLogger("buildbot")
        self.cfg = cfg
        self.url = self.cfg["buildbot"]
        self.builders = [ x for x in self.cfg["builders"] ]
        self.nbuilds = self.cfg["nbuilds"]
        self.revs = {}
        self.get_status()

    def get_status(self):
        self.log.info("loading build/test status from buildbot")
        for builder in self.builders:
            for (rev, b) in self.rev_build_pairs(builder):
                if rev not in self.revs:
                    self.revs[rev] = {}

                if "results" in b and (not build_has_status(b, BUILDBOT_STATUS_RETRY)):
                    if builder not in self.revs[rev]:
                        self.revs[rev][builder] = b

    def rev_build_pairs(self, builder):
        u = "%s/json/builders/%s/builds?%s" % \
            (self.url, builder,
             "&".join(["select=%d" % x
                       for x in range(-1, -(self.nbuilds+1), -1)]))
        self.log.info("fetching " + u)
        j = json.load(urllib.request.urlopen(u, timeout=TIMEOUT))
        for build in j:
            b = j[build]
            rev = None
            if "properties" not in b:
                continue
            for props in b["properties"]:
                if props[0] == "got_revision" and props[2] in ("Source", "Git", "SetProperty Step"):
                    rev = props[1]
            if rev is not None:
                yield (rev, b)

    # returns a pair: a tri-state (False=failure, True=pass, None=waiting)
    # coupled with two lists of URLs to post back as status-details. When
    # successful, the first URL-list is the successes and the second is the
    # warnings; when failing, the first URL-list is the failures and the
    # second is the exceptions.
    def test_status(self, sha):

        if sha in self.revs:

            passes = []
            warnings = []

            failures = []
            exceptions = []

            for builder in self.builders:

                if builder not in self.revs[sha]:
                    self.log.info("missing info for builder %s on %s"
                                  % (builder, sha))
                    continue

                self.log.info("checking results for %s on %s"
                              % (builder, sha))
                b = self.revs[sha][builder]
                if "results" in b:
                    self.log.info("got results %s for %s on %s"
                                  % (b["results"], builder, sha))
                    u = ("%s/builders/%s/builds/%s" %
                         (self.url, builder, b["number"]))
                    if build_has_status(b, BUILDBOT_STATUS_SUCCESS):
                        passes.append(u)
                    elif build_has_status(b, BUILDBOT_STATUS_WARNINGS):
                        warnings.append(u)
                    elif build_has_status(b, BUILDBOT_STATUS_FAILURE):
                        failures.append(u)
                    elif build_has_status(b, BUILDBOT_STATUS_EXCEPTION):
                        exceptions.append(u)

            if len(failures) > 0 or len(exceptions) > 0:
                return (False, failures, exceptions)

            elif len(passes) + len(warnings) == len(self.builders):
                return (True, passes, warnings)

            else:
                return (None, [], [])

        else:
            self.log.info("missing info sha %s" % sha)
            return (None, [], [])

def ustr(s):
    if s is None:
        return ""
    else:
        return s

class PullReq:
    def __init__(self, cfg, gh, j):
        self.cfg = cfg
        self.log = logging.getLogger("pullreq")
        self.user = cfg["gh_user"]
        self.target_ref = j["base"]["ref"]
        self.reviewers = [ r for r in cfg["reviewers"] ]
        self.approval_tokens = [ r for r in cfg["approval_tokens"] ]
        self.disapproval_tokens = [ r for r in cfg["disapproval_tokens"] ]
        self.ignored_users_in_comments = [ r for r in cfg.get("ignored_users_in_comments", []) ]
        self.num=j["number"]
        self.gh_host=cfg.get("gh_host", "github.com")
        self.dst_owner=cfg["owner"]
        self.dst_repo=cfg["repo"]
        self.src_owner=j["head"]["repo"]["owner"]["login"]
        self.src_repo=j["head"]["repo"]["name"]
        self.ref=j["head"]["ref"]
        self.sha=j["head"]["sha"]

        if cfg.get("test_ref"):
            self.test_ref = cfg["test_ref"]
        else:
            self.test_ref = '%s-integration-%s-%s' % (self.user, self.num, self.ref)

        self.title=ustr(j["title"])
        self.body=ustr(j["body"])
        self.merge_sha = None
        self.closed=j["state"] == "closed"
        self.approved = False
        self.testpass = False
        self.gh = gh

        # Not really, but github often lies about the result or returns
        # wrong data here, and we don't want to waste anyone's time with
        # "your patch bitrotted" when it hasn't.
        self.mergeable = True

        self.pull_comments = []
        self.head_comments = []
        self.get_pull_comments()
        self.get_head_comments()
        self.get_head_statuses()
        self.get_mergeable()
        self.get_merge_sha()
        self.loaded_ok = True


    def short(self):
        return ("%s/%s/%s = %.8s" %
                (self.src_owner, self.src_repo, self.ref, self.sha))

    def desc(self):
        return ("pull https://%s/%s/%s/pull/%d - %s - '%.30s'" %
                (self.gh_host, self.dst_owner, self.dst_repo,
                 self.num, self.short(), self.title))

    def src(self):
        return self.gh.repos(self.src_owner)(self.src_repo)

    def dst(self):
        return self.gh.repos(self.dst_owner)(self.dst_repo)

    def get_pull_comments(self):
        logging.info("loading pull and issue comments on pull #%d", self.num)
        cs = (self.dst().pulls(self.num).comments().get()
              + self.dst().issues(self.num).comments().get())
        self.pull_comments = [
            (c["created_at"],
             c["user"]["login"],
             ustr(c["body"]))
            for c in cs
            ]

    def get_head_comments(self):
        logging.info("loading head comments on %s", self.short())
        cs = self.src().commits(self.sha).comments().get()
        self.head_comments = [
            (c["created_at"],
             c["user"]["login"],
             ustr(c["body"]))
            for c in cs
            if c["user"]["login"] in self.reviewers and
                # don't allow edited comments because the owner of the fork can edit them
                c["created_at"] == c["updated_at"]
            ]

    def all_comments(self):
        a = self.head_comments + self.pull_comments
        a = sorted(a, key=lambda c: c[0])
        a = [c for c in a if c[1] not in self.ignored_users_in_comments]
        return a

    def last_comment(self):
        a = self.all_comments()
        if len(a) > 0:
            return a[-1]
        else:
            return ("","","")

    def approval_list(self):
        rec = re.compile(r"^(?:"+"|".join([re.escape(t) for t in self.approval_tokens])+")\s+([a-z0-9]{7,40})")
        return (
                # check for approval tokens on the commit comments
                [u for (d,u,c) in self.head_comments
                    if any([c.startswith(token) for token in self.approval_tokens])]
                 +
                # check for the r=<user> syntax on the commit comment
                [ m.group(1)
                  for (_,_,c) in self.head_comments
                  for m in [re.match(r"^r=([a-zA-Z0-9_-]+)", c)] if m ]
                +
                # check for the approval tokens followed by the branch SHA in the PR comments from reviewers
                [ u
                  for (_,u,c) in self.pull_comments
                  for m in [re.match(rec, c)] if m and u in self.reviewers and self.sha.startswith(m.group(1)) ]
                +
                # check for the r=<name> followed by the branch SHA in the PR comments from reviewers
                [ m.group(1)
                  for (_,_,c) in self.head_comments
                  for m in [re.match(r"^r=([a-zA-Z0-9_-]+) ([a-z0-9]+)", c)] if m and u in self.reviewers and self.sha.startswith(m.group(2)) ])

    def priority(self):
        p = 0
        for (d, u, c) in self.head_comments:
            m = re.search(r"\bp=(-?\d+)\b", c)
            if m is not None:
                p = max(p, int(m.group(1)))
        return p

    def prioritized_state(self):
        return (self.current_state(),
                self.priority(),
                -self.num)

    def disapproval_list(self):
        rec = re.compile(r"^(?:"+"|".join([re.escape(t) for t in self.disapproval_tokens])+")\s+([a-z0-9]{7,40})")
        return (
                # check for disapproval tokens on the commit comments
                [u for (d,u,c) in self.head_comments
                    if any([c.startswith(token) for token in self.disapproval_tokens])]
                +
                # check for disapproval tokens followed by the branch SHA in the PR comments from reviewers
                [ u
                    for (_,u,c) in self.pull_comments
                    for m in [re.match(rec, c)] if m and u in self.reviewers and self.sha.startswith(m.group(1)) ])

    def count_retries(self):
        return len([c for (d,u,c) in self.head_comments if (
                    c.startswith("@"+self.user+": retry"))])

    # annoyingly, even though we're starting from a "pull" json
    # blob, this blob does not have the "mergeable" flag; only
    # the one you get by re-requesting the _specific_ pull
    # comes with that. It also often returns None rather than
    # True or False. Yay.
    def get_mergeable(self):
        logging.info("loading mergeability of %d", self.num)
        self.mergeable = self.dst().pulls(self.num).get()["mergeable"]

    # github lets us externalize states as such:
    #
    # {no state}  -- we haven't seen a review yet. wait for r+ or r-
    # {pending} -- we saw r+ and are attempting to build & test
    # {failure} -- we saw a test failure. we post details, ignore.
    # {success} -- tests passed, time to move target-branch
    # {error} -- tests passed but merging failed (or other error)!

    def get_head_statuses(self):
        ss = self.dst().statuses(self.sha).get()
        logging.info("loading statuses of %s", self.short())
        self.statuses = [ s["state"]
                          for s in ss
                          if s["creator"]["login"] == self.user]

    def set_status(self, s, **kwargs):
        self.log.info("%s - setting status: %s (%s)",
                      self.short(), s, str(kwargs))
        self.dst().statuses(self.sha).post(state=s, **kwargs)

    def set_pending(self, txt, url):
        self.set_status("pending", description=txt, target_url=url)

    def set_success(self, txt, url):
        self.set_status("success", description=txt, target_url=url)

    def set_failure(self, txt, url):
        self.set_status("failure", description=txt, target_url=url)

    def set_error(self, txt):
        self.set_status("error", description=txt)

    def count_failures(self):
        return len([c for c in self.statuses if c == "failure"])

    def count_successes(self):
        return len([c for c in self.statuses if c == "success"])

    def count_pendings(self):
        return len([c for c in self.statuses if c == "pending"])

    def count_errors(self):
        return len([c for c in self.statuses if c == "error"])

    def merge_allowed(self):
        if self.cfg.get('no_auto_merge') is True:
            # bors is configured to wait for the PR author to approve the merge
            rec = re.compile(r"^@"+re.escape(self.user)+":{0,1} merge")
            merges = [ u
                    for (_,u,c) in self.pull_comments
                    for m in [re.match(rec, c)] if m and u in self.reviewers ]
            return len(merges) > 0
        return True

    def maybe_delete_source_branch(self):
        """
        If the `delete_source_branch` config option is set to True,
        this will delete the branch that is the source of this pull-request.
        The default config value is False. If the user running bors doesn't
        have contributor power over the repository the pull-request originated,
        it will not be able to delete the branch.
        """
        if self.cfg.get('delete_source_branch') is True:
            # Try to clean up the feature branch (the source of this PR)
            try:
                self.dst().git().refs().heads(self.ref).delete()
            except github.ApiError:
                self.log.info("deleting source branch %s failed" % self.test_ref)

    def get_merge_sha(self):
        # Find the newest 'pending' status and parse the SHA out of that
        ss = self.dst().statuses(self.sha).get()
        logging.info("loading statuses of %s", self.short())
        statusdescs = [ s["description"]
                          for s in ss
                          if s["creator"]["login"] == self.user and s["state"] == "pending"]
        if len(statusdescs) > 0:
            # parse it
            m = re.match(r"running tests for candidate ([a-z0-9]+)", statusdescs[0])
            if m:
                self.merge_sha = m.group(1)

    def current_state(self):

        if self.closed:
            return STATE_CLOSED

        if (self.count_errors() +
            self.count_failures()) > self.count_retries():
            return STATE_BAD

        if len(self.disapproval_list()) != 0:
            return STATE_BAD

        if self.count_successes() != 0:
            return STATE_TESTED

        if self.mergeable is False:
            return STATE_STALE

        if len(self.approval_list()) != 0:
            if self.count_pendings() <= self.count_retries():
                return STATE_APPROVED
            else:
                return STATE_PENDING

        if len(self.all_comments()) != 0:
            return STATE_DISCUSSING

        return STATE_UNREVIEWED

    # subtle: during a "pull req" github associates the sha1 with both src
    # and dst repos -- it's connected to a ref on both.
    #
    # due to github's UI, a review is going to always happen by an r+ on
    # the src repo. But we want to keep notes in the dst repo, both comments
    # and status we set.
    def add_comment(self, sha, comment):
        self.dst().commits(sha).comments().post(body=comment)


    # These are more destructive actions that affect the dst repo

    def reset_test_ref_to_target(self):
        j = self.dst().git().refs().heads(self.target_ref).get()
        target_sha = j["object"]["sha"]
        self.log.info("resetting %s to %s = %.8s",
                      self.test_ref, self.target_ref, target_sha)
        try:
            self.dst().git().refs().heads(self.test_ref).get()
            self.dst().git().refs().heads(self.test_ref).patch(sha=target_sha,
                                                                 force=True)
        except github.ApiError:
            self.dst().git().refs().post(sha=target_sha,
                    ref="refs/heads/"+self.test_ref)

    def merge_pull_head_to_test_ref(self):
        s = "merging %s into %s" % (self.short(), self.test_ref)
        try:
            self.log.info(s)
            self.add_comment(self.sha, s)
            m = ("Merge pull request #%d from %s/%s\n\n%s\n\nReviewed-by: %s" %
                 (self.num, self.src_owner, self.ref,
                  self.title, ",".join(self.approval_list())))
            j = self.dst().merges().post(base=self.test_ref,
                                         head=self.sha,
                                         commit_message=m)
            self.merge_sha = j["sha"]
            u = ("https://%s/%s/%s/commit/%s" %
                 (self.gh_host, self.dst_owner, self.dst_repo, self.merge_sha))
            s = "%s merged ok, testing candidate = %.8s" % (self.short(),
                                                            self.merge_sha)
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_pending("running tests for candidate %s" % self.merge_sha, u)

        except github.ApiError:
            s = s + " failed"
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_error(s)

    def advance_target_ref_to_test(self):
        assert self.merge_sha is not None
        s = ("fast-forwarding %s to %s = %.8s" %
             (self.target_ref, self.test_ref, self.merge_sha))
        self.log.info(s)
        try:
            self.dst().git().refs().heads(self.target_ref).patch(sha=self.merge_sha,
                                                                 force=False)
            self.add_comment(self.sha, s)

            # Try to clean up the temporary integration branch
            try:
                self.dst().git().refs().heads(self.test_ref).delete()
            except github.ApiError:
                self.log.info("deleting integration branch %s failed" % self.test_ref)

            self.maybe_delete_source_branch()

        except github.ApiError:
            s = s + " failed"
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_error(s)

    def fresh(self):
        # NOTE: only call this when needed,
        # as the result may change as other
        # PRs are advanced

        # a PR is fresh if the two
        # parents of the merge sha are
        # the tip of the merge-target and the
        # feature branch
        owner = self.cfg["owner"]
        repo = self.cfg["repo"]
        target_head = self.gh.repos(owner)(repo).git().refs().heads(self.target_ref).get()
        target_sha = target_head["object"]["sha"]
        test_commit = self.gh.repos(owner)(repo).git().commits(self.merge_sha).get()
        test_parents = [ x["sha"] for x in test_commit["parents"] ]
        return (len(test_parents) == 2 and
                target_sha in test_parents and
                self.sha in test_parents)

    def try_advance(self):
        s = self.current_state()

        self.log.info("considering %s", self.desc())

        if s == STATE_UNREVIEWED or s == STATE_DISCUSSING:
            self.log.info("%s - waiting on review", self.short())

        elif s == STATE_APPROVED:
            self.log.info("%s - found approval, advancing to test", self.short())
            self.add_comment(self.sha, ("saw approval from "
                                        + ", ".join(self.approval_list())
                                        + ("\nat https://%s/%s/%s/commit/%s" %
                                             (self.gh_host,
                                              self.src_owner,
                                              self.src_repo,
                                              self.sha))))

            self.reset_test_ref_to_target()
            self.merge_pull_head_to_test_ref()

        elif s == STATE_PENDING:
            # Make sure the optional merge sha is loaded
            owner = self.cfg["owner"]
            repo = self.cfg["repo"]
            test_head = self.gh.repos(owner)(repo).git().refs().heads(self.test_ref).get()
            test_sha = test_head["object"]["sha"]
            self.merge_sha = test_sha

            if not self.fresh():
                c = ("Merge sha %.8s is stale."
                     % (self.merge_sha,))
                self.log.info(c)
                self.add_comment(self.sha, c)
                self.reset_test_ref_to_target()
                self.merge_pull_head_to_test_ref()
                return
            self.log.info("%s - found pending state, checking tests", self.short())
            assert self.merge_sha is not None
            if self.cfg.get("use_github_checks_api"):
                statuses = self.dst().commits(self.merge_sha,"check-runs").get().check_runs
                self.log.info("USING %d commit status for commit: %s" % (len(statuses), self.merge_sha))
                pending = [s for s in statuses if s["status"] != "completed"]
                successes = [s for s in statuses if s["status"] == "completed" and s["conclusion"] == "success"]
                failures = [s for s in statuses if s["status"] == "completed" and s["conclusion"] == "failure"]
                # consider any other completion code as error
                errors = [s for s in statuses if s["status"] == "completed" and not ( s["conclusion"] == "failure" or s["conclusion"] == "success") ]
                self.log.info("%d pending %d sucesses %d failure %d error" % (len(pending), len(successes), len(failures), len(errors)))

                if len(statuses) == 0 or (len(successes) + len(failures) + len(errors)) == 0:
                    t = None
                    main_urls = []
                    extra_urls = []
                elif len(successes) > 0 and len(statuses) == len(successes):
                    # should it be
                    # "len(successes) > 0 and (len(failures) + len(errors)) == 0" ?
                    t = True
                    main_urls = [s["html_url"] for s in successes]
                    extra_urls = []
                else:
                    t = False
                    main_urls = [s["html_url"] for s in failures]
                    extra_urls = [s["html_url"] for s in errors]
            if self.cfg.get("use_github_commit_status_api"):
                statuses = self.dst().statuses(self.merge_sha).get()
                self.log.info("found %d commit status for commit: %s" % (len(statuses), self.merge_sha))
                pending = [s for s in statuses if s["state"] == "pending"]
                successes = [s for s in statuses if s["state"] == "success"]
                failures = [s for s in statuses if s["state"] == "failure"]
                errors = [s for s in statuses if s["state"] == "error"]
                self.log.info("%d pending %d sucesses %d failure %d error" % (len(pending), len(successes), len(failures), len(errors)))
                if len(statuses) == 0 or (len(successes) + len(failures) + len(errors)) == 0:
                    t = None
                    main_urls = []
                    extra_urls = []
                elif len(successes) > 0 and (len(failures) + len(errors)) == 0:
                    t = True
                    main_urls = [s["target_url"] for s in successes]
                    extra_urls = []
                else:
                    t = False
                    main_urls = [s["target_url"] for s in failures]
                    extra_urls = [s["target_url"] for s in errors]
            else:
                bb = BuildBot(self.cfg)
                (t, main_urls, extra_urls) = bb.test_status(self.merge_sha)

            if t is True:
                self.log.info("%s - tests passed, marking success", self.short())
                c = "all tests pass:"
                for url in main_urls:
                    c += "\nsuccess: " + url 
                for url in extra_urls:
                    c += "\nwarning: " + url
                c += "\n"
                self.add_comment(self.sha, c)
                self.set_success("all tests passed", url)

            elif t is False:
                self.log.info("%s - tests failed, marking failure", self.short())
                c = "some tests failed:"
                for url in main_urls:
                    c += "\nfailure: " + url 
                for url in extra_urls:
                    c += "\nexception: " + url
                c += "\n"
                self.add_comment(self.sha, c)
                self.set_failure("some tests failed", url)

            else:
                self.log.info("%s - no info yet, waiting on tests", self.short())

        elif s == STATE_TESTED:
            if not self.merge_allowed():
                self.log.info("%s - tests successful, waiting for merge approval",
                        self.short())
                return
            if self.fresh():
                self.log.info("%s - tests successful, attempting landing", self.short())
                self.advance_target_ref_to_test()
            else:
                c = ("Merge sha %.8s is stale."
                     % (self.merge_sha,))
                self.log.info(c)
                self.add_comment(self.sha, c)
                self.reset_test_ref_to_target()
                self.merge_pull_head_to_test_ref()



def main():

    fmt = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(message)s',
                            datefmt="%Y-%m-%d %H:%M:%S %Z")

    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--quiet", help="Be quiet", action='store_true')
    parser.add_argument("-r", "--repo", help="Repo to operate on - overrides configured repo")
    args = parser.parse_args()
    if not args.quiet:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG)
        logging.root.addHandler(sh)


    rfh = logging.handlers.RotatingFileHandler("bors.log",
                                               backupCount=10,
                                               maxBytes=1000000)
    rfh.setFormatter(fmt)
    rfh.setLevel(logging.DEBUG)
    logging.root.addHandler(rfh)
    logging.root.setLevel(logging.DEBUG)

    logging.info("---------- starting run ----------")
    logging.info("loading bors.cfg")
    cfg = json.load(open("bors.cfg"))

    if 'approval_tokens' not in cfg:
        cfg['approval_tokens'] = ['r+', 'r=me']
    if 'disapproval_tokens' not in cfg:
        cfg['disapproval_tokens'] = ['r-']


    if args.repo:
        logging.info("using command line repo %s", args.repo)
        cfg["repo"] = args.repo

    gh = None
    if "gh_pass" in cfg:
        gh = github.GitHub(username=cfg["gh_user"],
                           password=cfg["gh_pass"],
                           api_url=cfg.get("gh_api"))
    else:
        gh = github.GitHub(username=cfg["gh_user"],
                           access_token=cfg["gh_token"],
                           api_url=cfg.get("gh_api"))


    owner = cfg["owner"]
    repo = cfg["repo"]

    if "collaborators_as_reviewers" in cfg and cfg["collaborators_as_reviewers"] is True:
        # NOTE there is no paging when listing collaborators
        collabs = gh.repos(owner)(repo).collaborators().get()
        cfg["reviewers"] = [c["login"] for c in collabs]
        logging.info("found %d collaborators", len(collabs))

    pulls = gh.repos(owner)(repo).pulls().get()

    pulls = [ PullReq(cfg, gh, pull) for pull in
              pulls ]

    #
    # We are reconstructing the relationship between three tree-states on the
    # fly here. We're doing so because there was nowhere useful to leave it
    # written between runs, and it can be discovered by inspection.
    #
    # The situation is this:
    #
    #
    #                 test_ref ==>  <test_sha>
    #
    #                               /     \
    #                              /       \
    #                             /         \
    #  target_ref ==>  <target_sha>         <candidate_sha> == p.sha
    #                            |           |
    #                            |           |
    #                           ...         ...
    #
    #
    # When this is true, it means we're currently testing candidate_sha
    # which (should) be the sha of a single pull req's head.
    #
    # We discover this situation by working backwards:
    #
    #   - We get the test_ref's sha, test_sha
    #   - We get the target_ref's sha, target_sha
    #   - We get the 2 parent links of test_sha
    #   - We exclude the target_sha from that parent list
    #   - Whatever the _other_ parent is, we consider the candidate
    #
    # If we fail to find any steps along the way, bors will either ignore
    # the current state of affairs (i.e. assume it's _not_ presently testing
    # any pull req) or else crash due to inability to load something.
    # So it's non-fatal if we get it wrong; we'll only advance (make changes)
    # if we get it right.
    #

    # By now we have found all pull reqs and marked the one that's the
    # currently-building candidate (if it exists). We then sort them
    # by ripeness and pick the one closest to landing, try to push it
    # along one step.
    #
    # We also apply a secondary sort order that lets the reviewers prioritize
    # incoming pulls by putting p=<num>, with the num default to 0. Lower
    # numbers are less important, higher are more important. Also sort by
    # negative pull-req number; this is an approximation of "oldest first"
    # that avoids trying to reason about dates.

    pulls = sorted(pulls, key=PullReq.prioritized_state)
    logging.info("got %d open pull reqs", len(pulls))

    # Dump state-of-world javascript fragment

    try:
        json_db = json.load(open('bors-status.json', 'r'))
    except IOError:
        json_db = {}

    j = []
    for pull in pulls:
        j.append({ "num": pull.num,
                   "title": pull.title,
                   "body": pull.body,
                   "prio": pull.priority(),
                   "src_owner": pull.src_owner,
                   "src_repo": pull.src_repo,
                   "dst_owner": pull.dst_owner,
                   "dst_repo": pull.dst_repo,
                   "num_comments": len(pull.head_comments +
                                       pull.pull_comments),
                   "last_comment": pull.last_comment(),
                   "approvals": pull.approval_list(),
                   "ref": pull.ref,
                   "sha": pull.sha,
                   "state": state_name(pull.current_state()) })

    json_db[repo] = j
    json.dump(json_db, open('bors-status.json', 'w'))

    f = open("bors-status.js", "w")
    f.write(strftime('var updated = new Date("%Y-%m-%dT%H:%M:%SZ");\n',
                     gmtime()))
    f.write("var bors = ")
    json.dump(json_db, f)
    f.write(";\n")
    f.close()


    pulls = [p for p in pulls if (p.current_state() >= STATE_DISCUSSING
                                  and p.current_state() < STATE_CLOSED) ]

    logging.info("got %d viable pull reqs", len(pulls))
    for pull in pulls:
        logging.info("(%d,%d) : %s",
                     pull.current_state(),
                     pull.priority(),
                     pull.desc())

    max_pulls_per_run = cfg.get('max_pulls_per_run')
    if max_pulls_per_run:
        logging.info("Only considering %d pull-requests this run", max_pulls_per_run)
        pulls = pulls[-max_pulls_per_run:]

    [p.try_advance() for p in reversed(pulls)]

if __name__ == "__main__":
    try:
        main()
    except github.ApiError as e:
        print(("Github API exception: " + str(e.response)))
        exit(-1)
