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
# Bors is an automated integrator for github and buildbot.
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
#       "master_ref": "<git-ref-for-integration>",
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
#       "master_ref": "incoming",
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
#   - if state==APPROVED, merge pull.sha + master => test_ref:
#     - if merge ok, set PENDING
#     - if merge fail, set ERROR (pull req bitrotted)
#
#   - if state==PENDING, look at buildbot for test results:
#     - if failed, set FAILED
#     - if passed, set TESTED
#     (if no test status, exit; waiting for results)
#
#   - if state==TESTED, fast-forward master to test_ref
#     - if ffwd works, close pull req
#     - if ffwd fails, set ERROR (someone moved master on us)

import json
import urllib2
import sys
import re
import logging, logging.handlers
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
        self.url = self.cfg["buildbot"].encode("utf8")
        self.builders = [ x.encode("utf8") for x in self.cfg["builders"] ]
        self.nbuilds = self.cfg["nbuilds"]
        self.revs = {}
        self.get_status()

    def get_status(self):
        self.log.info("loading build/test status from buildbot")
        for builder in self.builders:
            for (rev, b) in self.rev_build_pairs(builder):
                if not (rev in self.revs):
                    self.revs[rev] = {}

                if "results" in b and (not build_has_status(b, BUILDBOT_STATUS_RETRY)):
                    if not (builder in self.revs[rev]):
                        self.revs[rev][builder] = b

    def rev_build_pairs(self, builder):
        u = "%s/json/builders/%s/builds?%s" % \
            (self.url, builder,
             "&".join(["select=%d" % x
                       for x in range(-1, -(self.nbuilds+1), -1)]))
        self.log.info("fetching " + u)
        j = json.load(urllib2.urlopen(u, timeout=TIMEOUT))
        for build in j:
            b = j[build]
            rev = None
            if "properties" not in b:
                continue
            for props in b["properties"]:
                if props[0] == "got_revision" and props[2] in ("Source", "Git", "SetProperty Step"):
                    rev = props[1].encode("utf8")
            if rev != None:
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
    if s == None:
        return ""
    else:
        return s.encode("utf8")

class PullReq:
    def __init__(self, cfg, gh, j):
        self.cfg = cfg
        self.log = logging.getLogger("pullreq")
        self.user = cfg["gh_user"].encode("utf8")
        self.test_ref = cfg["test_ref"].encode("utf8")
        self.master_ref = cfg["master_ref"].encode("utf8")
        self.batch_ref = cfg.get('batch', 'batch')
        self.reviewers = [ r.encode("utf8") for r in cfg["reviewers"] ]
        self.num=j["number"]
        self.dst_owner=cfg["owner"].encode("utf8")
        self.dst_repo=cfg["repo"].encode("utf8")
        self.src_owner=j["head"]["repo"]["owner"]["login"].encode("utf8")
        self.src_repo=j["head"]["repo"]["name"].encode("utf8")
        self.ref=j["head"]["ref"].encode("utf8")
        self.sha=j["head"]["sha"].encode("utf8")
        self.title=ustr(j["title"])
        self.body=ustr(j["body"])
        self.closed=j["state"].encode("utf8") == "closed"
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
        self.loaded_ok = True


    def short(self):
        return ("%s/%s/%s = %.8s" %
                (self.src_owner, self.src_repo, self.ref, self.sha))

    def desc(self):
        return ("pull https://github.com/%s/%s/pull/%d - %s - '%.30s'" %
                (self.dst_owner, self.dst_repo,
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
            (c["created_at"].encode("utf8"),
             c["user"]["login"].encode("utf8"),
             ustr(c["body"]))
            for c in cs
            ]

    def get_head_comments(self):
        logging.info("loading head comments on %s", self.short())
        cs = self.src().commits(self.sha).comments().get()
        self.head_comments = [
            (c["created_at"].encode("utf8"),
             c["user"]["login"].encode("utf8"),
             ustr(c["body"]))
            for c in cs
            if c["user"]["login"].encode("utf8") in self.reviewers
            ]

    def all_comments(self):
        a = self.head_comments + self.pull_comments
        a = sorted(a, key=lambda c: c[0])
        return a

    def last_comment(self):
        a = self.all_comments()
        if len(a) > 0:
            return a[-1]
        else:
            return ("","","")

    def approval_list(self):
        return ([u for (d,u,c) in self.head_comments
                 if (c.startswith("r+") or
                     c.startswith("r=me"))] +
                [ m.group(1)
                  for (_,_,c) in self.head_comments
                  for m in [re.match(r"^r=(\w+)", c)] if m ])

    def batched(self):
        for date, user, comment in self.head_comments:
            if re.search(r'\b(?:rollup|batch)\b', comment):
                return True
        return False

    def priority(self):
        p = -1 if self.batched() else 0

        for (d, u, c) in self.head_comments:
            m = re.search(r"\bp=(-?\d+)\b", c)
            if m != None:
                p = max(p, int(m.group(1)))
        return p

    def prioritized_state(self):
        return (self.current_state(),
                self.priority(),
                -self.num)

    def disapproval_list(self):
        return [u for (d,u,c) in self.head_comments
                if c.startswith("r-")]

    def count_retries(self):
        return len([c for (d,u,c) in self.head_comments if (
                    c.startswith("@bors: retry"))])

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
    # {success} -- tests passed, time to move master
    # {error} -- tests passed but merging failed (or other error)!

    def get_head_statuses(self):
        ss = self.dst().statuses(self.sha).get()
        logging.info("loading statuses of %s", self.short())
        self.statuses = [ s["state"].encode("utf8")
                          for s in ss
                          if s["creator"]["login"].encode("utf8") == self.user]

    def set_status(self, s, **kwargs):
        self.log.info("%s - setting status: %s (%s)",
                      self.short(), s, str(kwargs))
        self.dst().statuses(self.sha).post(state=s, **kwargs)

    def set_pending(self, txt, url):
        self.set_status("pending", description=txt, target_url=url)

    def set_success(self, txt):
        self.set_status("success", description=txt)

    def set_failure(self, txt):
        self.set_status("failure", description=txt)

    def set_error(self, txt):
        self.set_status("error", description=txt)

    def count_failures(self):
        return len([c for c in self.statuses if c == "failure"])

    def count_successes(self):
        return 1 if self.statuses and self.statuses[0] == 'success' else 0

    def count_pendings(self):
        return len([c for c in self.statuses if c == "pending"])

    def count_errors(self):
        return len([c for c in self.statuses if c == "error"])

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

        if self.mergeable == False:
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

    def reset_test_ref_to_master(self):
        j = self.dst().git().refs().heads(self.master_ref).get()
        master_sha = j["object"]["sha"].encode("utf8")
        self.log.info("resetting %s to %s = %.8s",
                      self.test_ref, self.master_ref, master_sha)
        self.dst().git().refs().heads(self.test_ref).patch(sha=master_sha,
                                                           force=True)

    def parse_metadata(self):
        cs = self.dst().commits(self.sha).comments().get()
        status_comments = [
            c['body'][len(u'status: '):].encode('utf-8')
            for c in cs
            if c['user']['login'].encode('utf-8') == self.user and c['body'] and c['body'].startswith(u'status: ')
        ]
        self.metadata = json.loads(status_comments[-1]) if status_comments else {}

    def set_metadata(self, **kwargs):
        self.add_comment(self.sha, 'status: {}'.format(json.dumps(kwargs)))

    def merge_pull_head_to_test_ref(self):
        s = "merging %s into %s" % (self.short(), self.test_ref)
        try:
            self.log.info(s)
            self.add_comment(self.sha, s)
            m = ("auto merge of #%d : %s/%s/%s, r=%s\n\n%s" %
                 (self.num, self.src_owner, self.src_repo, self.ref,
                  ",".join(self.approval_list()), self.body))
            j = self.dst().merges().post(base=self.test_ref,
                                         head=self.sha,
                                         commit_message=m)
            merge_sha = j["sha"].encode("utf8")
            u = ("https://github.com/%s/%s/commit/%s" %
                 (self.dst_owner, self.dst_repo, merge_sha))
            s = "%s merged ok, testing candidate = %.8s" % (self.short(),
                                                            merge_sha)
            self.log.info(s)
            self.set_metadata(merge_sha=merge_sha)
            self.set_pending("running tests for candidate {:.7}".format(merge_sha), u)
            self.add_comment(self.sha, s)

        except github.ApiError:
            s = s + " failed"
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_error(s)

    def merge_batched_pull_reqs_to_test_ref(self, pulls):
        batched_pulls = [x for x in pulls if x.batched() and x.current_state() == STATE_APPROVED]

        batch_msg = 'merging {} batched pull requests into {}'.format(
            len(batched_pulls),
            self.batch_ref,
        )
        self.log.info(batch_msg)
        self.add_comment(self.sha, batch_msg)

        info = self.dst().git().refs().heads(self.master_ref).get()
        master_sha = info['object']['sha'].encode('utf-8')
        try:
            self.dst().git().refs().heads(self.batch_ref).get()
            self.dst().git().refs().heads(self.batch_ref).patch(sha=master_sha, force=True)
        except github.ApiError:
            self.dst().git().refs().post(sha=master_sha, ref='refs/heads/' + self.batch_ref)

        successes = []
        failures = []
        rollup_pulls = []

        batch_sha = ''

        for pull in batched_pulls:
            self.log.info('merging {} into {}'.format(pull.short(), self.batch_ref))

            msg = 'Merge pull request #{} from {}/{}\n\n{}\n\nReviewed-by: {}'.format(
                pull.num,
                pull.src_owner, pull.ref,
                pull.title,
                ', '.join(pull.approval_list())
            )
            pull_repr = '- #{} {} ({}/{} = {})'.format(pull.num, pull.title, pull.src_owner, pull.ref, pull.sha)

            try:
                info = self.dst().merges().post(base=self.batch_ref, head=pull.sha, commit_message=msg)
                batch_sha = info['sha'].encode('utf-8')
            except github.ApiError:
                failures.append(pull_repr)
            else:
                successes.append(pull_repr)
                rollup_pulls.append([pull.num, pull.sha])

        if batch_sha:
            try:
                self.dst().git().refs().heads(self.test_ref).get()
                self.dst().git().refs().heads(self.test_ref).patch(sha=batch_sha)
            except github.ApiError as e:
                self.dst().git().refs().post(sha=batch_sha, ref='refs/heads/' + self.test_ref)

            url = 'https://github.com/{}/{}/commit/{}'.format(self.dst_owner, self.dst_repo, batch_sha)
            short_msg = 'running tests for rollup candidate {:.7} (successful merges: {} out of {})'.format(
                batch_sha,
                len(successes),
                len(successes) + len(failures),
            )
            msg = 'Testing rollup candidate = {:.7}'.format(batch_sha)
            if successes: msg += '\n\n**Successful merges:**\n\n{}'.format('\n'.join(successes))
            if failures: msg += '\n\n**Failed merges:**\n\n{}'.format('\n'.join(failures))

            self.log.info(short_msg)
            self.set_metadata(merge_sha=batch_sha, rollup_pulls=rollup_pulls)
            self.set_pending(short_msg, url)
            self.add_comment(self.sha, msg)
        else:
            batch_msg += ' failed'

            self.log.info(batch_msg)
            self.add_comment(self.sha, batch_msg)
            self.set_error(batch_msg)

    def merge_or_batch(self, pulls):
        self.reset_test_ref_to_master()
        if self.batched():
            self.merge_batched_pull_reqs_to_test_ref(pulls)
        else:
            self.merge_pull_head_to_test_ref()

    def advance_master_ref_to_test(self, pulls):
        if self.batched():
            num2sha = {x.num: x.sha for x in pulls}

            advanced = False
            for num, sha in self.metadata['rollup_pulls']:
                if num2sha[num] != sha:
                    advanced = True

                    msg = '#{} advanced, testing again without the PR'.format(num)
                    self.log.info(msg)
                    self.add_comment(self.sha, msg)

            if advanced:
                self.statuses = [x for x in self.statuses if x not in ['success', 'pending']] # Mark this PR as unsuccessful
                self.merge_or_batch(pulls)
                return

        s = ("fast-forwarding %s to %s = %.8s" %
             (self.master_ref, self.test_ref, self.metadata['merge_sha']))
        self.log.info(s)
        try:
            self.dst().git().refs().heads(self.master_ref).patch(sha=self.metadata['merge_sha'],
                                                                 force=False)
            self.add_comment(self.sha, s)
        except github.ApiError:
            s = s + " failed"
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_error(s)

        try:
            self.dst().pulls(self.num).patch(state="closed")
            self.closed = True
        except github.ApiError:
            self.log.info("closing failed; auto-closed after merge?")
            pass



    def try_advance(self, pulls):
        s = self.current_state()

        self.log.info("considering %s", self.desc())

        if s == STATE_UNREVIEWED or s == STATE_DISCUSSING:
            self.log.info("%s - waiting on review", self.short())

        elif s == STATE_APPROVED:
            self.log.info("%s - found approval, advancing to test", self.short())
            self.add_comment(self.sha, ("saw approval from "
                                        + ", ".join(self.approval_list())
                                        + ("\nat https://github.com/%s/%s/commit/%s" %
                                             (self.src_owner,
                                              self.src_repo,
                                              self.sha))))

            self.merge_or_batch(pulls)

        elif s == STATE_PENDING:
            self.parse_metadata()
            self.log.info("%s - found pending state, checking tests", self.short())
            bb = BuildBot(self.cfg)
            (t, main_urls, extra_urls) = bb.test_status(self.metadata['merge_sha'])

            if t == True:
                self.log.info("%s - tests passed, marking success", self.short())
                c = "all tests pass:"
                for url in main_urls:
                    c += "\nsuccess: " + url 
                for url in extra_urls:
                    c += "\nwarning: " + url
                c += "\n"
                self.add_comment(self.sha, c)
                self.set_success("all tests passed")

            elif t == False:
                self.log.info("%s - tests failed, marking failure", self.short())
                c = "some tests failed:"
                for url in main_urls:
                    c += "\nfailure: " + url 
                for url in extra_urls:
                    c += "\nexception: " + url
                c += "\n"
                self.add_comment(self.sha, c)
                self.set_failure("some tests failed")

            else:
                self.log.info("%s - no info yet, waiting on tests", self.short())

        elif s == STATE_TESTED:
            self.parse_metadata()
            self.log.info("%s - tests successful, attempting landing", self.short())
            self.advance_master_ref_to_test(pulls)



def main():

    fmt = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(message)s',
                            datefmt="%Y-%m-%d %H:%M:%S %Z")

    if "--quiet" not in sys.argv:
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

    gh = None
    if "gh_pass" in cfg:
        gh = github.GitHub(username=cfg["gh_user"].encode("utf8"),
                           password=cfg["gh_pass"].encode("utf8"))
    else:
        gh = github.GitHub(username=cfg["gh_user"].encode("utf8"),
                           access_token=cfg["gh_token"].encode("utf8"))


    owner = cfg["owner"].encode("utf8")
    repo = cfg["repo"].encode("utf8")

    more_pulls = True
    all_pulls = []
    page = 1
    while more_pulls:
        logging.info("loading pull reqs (page %d)", page)
        pulls = gh.repos(owner)(repo).pulls().get(per_page=100,
                                                  page=page)
        all_pulls.extend(pulls)
        if len(pulls) == 0:
            more_pulls = False
        page += 1

    pulls = [ PullReq(cfg, gh, pull) for pull in
              all_pulls ]

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
    j = []
    for pull in pulls:
        j.append({ "num": pull.num,
                   "title": pull.title,
                   "body": pull.body,
                   "prio": pull.priority(),
                   "src_owner": pull.src_owner,
                   "src_repo": pull.src_repo,
                   "num_comments": len(pull.head_comments +
                                       pull.pull_comments),
                   "last_comment": pull.last_comment(),
                   "approvals": pull.approval_list(),
                   "ref": pull.ref,
                   "sha": pull.sha,
                   "state": state_name(pull.current_state()) })
    f = open("bors-status.js", "w")
    f.write(strftime('var updated = new Date("%Y-%m-%dT%H:%M:%SZ");\n',
                     gmtime()))
    f.write("var bors = ")
    json.dump(j, f)
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

    if len(pulls) == 0:
        logging.info("no pull requests open")
    else:
        p = pulls[-1]
        logging.info("working with most-ripe pull %s", p.short())
        p.try_advance(list(reversed(pulls)))



if __name__ == "__main__":
    try:
        main()
    except github.ApiError as e:
        print("Github API exception: " + str(e.response))
        exit(-1)

