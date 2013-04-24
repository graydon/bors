#!/usr/bin/env python

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
#	"owner": "<github-username-the-owner-of-repo>",
#	"repo": "<short-github-repo-name>",
#	"reviewers": ["<user1>", "<user2>", ...],
#	"builders": ["<buildbot-builder1>", "<buildbot-builder2>", ...],
#	"test_ref": "<git-ref-for-testing>",
#	"master_ref": "<git-ref-for-integration>",
#	"nbuilds": <number-of-buildbot-builds-history-to-look-at>,
#	"buildbot": "<buildbot-url>",
#	"gh_user": "<github-user-to-run-as>",
#	"gh_pass": "<password-for-that-user>"
# }
#
# For example, the rust config at the time of writing (minus password) is:
#
# {
#	"owner": "mozilla",
#	"repo": "rust",
#	"reviewers": ["brson", "catamorphism", "graydon", "nikomatsakis", "pcwalton"],
#	"builders": ["auto-linux", "auto-win", "auto-bsd", "auto-mac"],
#	"test_ref": "auto",
#	"master_ref": "incoming",
#	"nbuilds": 5,
#	"buildbot": "http://buildbot.rust-lang.org",
#	"gh_user": "bors",
#	"gh_pass": "..."
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
#   - if state==UNREVIEWED, look for r+ or r-:
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

__version__ = '1.0'

TIMEOUT=60

STATE_BAD = -1
STATE_UNREVIEWED = 0
STATE_APPROVED = 1
STATE_PENDING = 2
STATE_TESTED = 3
STATE_CLOSED = 4

def state_name(n):
    assert STATE_BAD <= n
    assert n <= STATE_CLOSED
    return [ "BAD",
             "UNREVIEWED",
             "APPROVED",
             "PENDING",
             "TESTED",
             "CLOSED" ][n+1]

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
                self.revs[rev][builder] = b

    def rev_build_pairs(self, builder):
        u = "%s/json/builders/%s/builds?%s" % \
            (self.url, builder,
             "&".join(["select=%d" % x
                       for x in range(-1, -(self.nbuilds+1), -1)]))
        j = json.load(urllib2.urlopen(u, timeout=TIMEOUT))
        for build in j:
            b = j[build]
            rev = None
            if "properties" not in b:
                continue
            for props in b["properties"]:
                if props[0] == "got_revision" and props[2] in ("Source", "Git"):
                    rev = props[1].encode("utf8")
            if rev != None:
                yield (rev, b)

    # returns a pair: a tri-state (False=failure, True=pass, None=waiting)
    # coupled with a list of URLs to post back as status-details
    def test_status(self, sha):

        if sha in self.revs:

            for builder in self.builders:
                if builder not in self.revs[sha]:
                    return (None, [])

            passes = []
            failures = []

            for builder in self.builders:
                b = self.revs[sha][builder]
                if "results" in b:
                    u = ("%s/builders/%s/builds/%s" %
                         (self.url, builder, b["number"]))
                    if b["results"] == 0 or b["results"] == 1:
                        passes.append(u)
                    elif b["results"] == 2 or b["results"] == 4:
                        failures.append(u)

            if len(failures) > 0:
                return (False, failures)

            elif len(passes) == len(self.builders):
                return (True, passes)

            else:
                return (None, [])

        else:
            return (None, [])



class PullReq:
    def __init__(self, cfg, gh, j):
        self.cfg = cfg
        self.log = logging.getLogger("pullreq")
        self.user = cfg["gh_user"].encode("utf8")
        self.test_ref = cfg["test_ref"].encode("utf8")
        self.master_ref = cfg["master_ref"].encode("utf8")
        self.reviewers = [ r.encode("utf8") for r in cfg["reviewers"] ]
        self.num=j["number"]
        self.dst_owner=cfg["owner"].encode("utf8")
        self.dst_repo=cfg["repo"].encode("utf8")
        self.src_owner=j["head"]["repo"]["owner"]["login"].encode("utf8")
        self.src_repo=j["head"]["repo"]["name"].encode("utf8")
        self.ref=j["head"]["ref"].encode("utf8")
        self.sha=j["head"]["sha"].encode("utf8")
        self.title=j["title"].encode("utf8")
        self.body=j["body"].encode("utf8")
        self.merge_sha = None
        self.closed=j["state"].encode("utf8") == "closed"
        self.approved = False
        self.testpass = False
        self.gh = gh
        self.get_comments()
        self.get_statuses()

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

    def get_comments(self):
        logging.info("loading comments on %s", self.short())
        cs = self.src().commits(self.sha).comments().get()
        self.comments = [
            (c["user"]["login"].encode("utf8"),
             c["body"].encode("utf8"))
            for c in cs
            if c["user"]["login"].encode("utf8") in self.reviewers
            ]

    def approval_list(self):
        return [u for (u,c) in self.comments
                if (c.startswith("r+") or
                    c.startswith("r=me"))]

    def priority(self):
        p = 0
        for (u, c) in self.comments:
            m = re.search(r"\bp=(-?\d+)\b", c)
            if m != None:
                p = max(p, int(m.group(1)))
        return p

    def prioritized_state(self):
        return (self.current_state(),
                self.priority(),
                -self.num)

    def disapproval_list(self):
        return [u for (u,c) in self.comments
                if c.startswith("r-")]

    def count_retries(self):
        return len([c for (u,c) in self.comments if (
                    c.startswith("@bors: retry"))])

    # github lets us externalize states as such:
    #
    # {no state}  -- we haven't seen a review yet. wait for r+ or r-
    # {pending} -- we saw r+ and are attempting to build & test
    # {failure} -- we saw a test failure. we post details, ignore.
    # {success} -- tests passed, time to move master
    # {error} -- tests passed but merging failed (or other error)!

    def get_statuses(self):
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
        return len([c for c in self.statuses if c == "success"])

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

        if len(self.approval_list()) != 0:
            if self.count_pendings() <= self.count_retries():
                return STATE_APPROVED
            else:
                return STATE_PENDING

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
            self.merge_sha = j["sha"].encode("utf8")
            u = ("https://github.com/%s/%s/commit/%s" %
                 (self.dst_owner, self.dst_repo, self.merge_sha))
            s = "%s merged ok, testing candidate = %.8s" % (self.short(),
                                                            self.merge_sha)
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_pending("running tests", u)

        except github.ApiError:
            s = s + " failed"
            self.log.info(s)
            self.add_comment(self.sha, s)
            self.set_error(s)

    def advance_master_ref_to_test(self):
        assert self.merge_sha != None
        s = ("fast-forwarding %s to %s = %.8s" %
             (self.master_ref, self.test_ref, self.merge_sha))
        self.log.info(s)
        try:
            self.dst().git().refs().heads(self.master_ref).patch(sha=self.merge_sha,
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



    def try_advance(self):
        s = self.current_state()

        self.log.info("considering %s", self.desc())

        if s == STATE_UNREVIEWED:
            self.log.info("%s - waiting on review", self.short())

        elif s == STATE_APPROVED:
            self.log.info("%s - found approval, advancing to test", self.short())
            self.add_comment(self.sha, ("saw approval from "
                                        + ", ".join(self.approval_list())
                                        + ("\nat https://github.com/%s/%s/commit/%s" %
                                             (self.src_owner,
                                              self.src_repo,
                                              self.sha))))

            self.reset_test_ref_to_master()
            self.merge_pull_head_to_test_ref()

        elif s == STATE_PENDING:
            if self.merge_sha == None:
                c = ("No active merge of candidate %.8s found, likely manual push to %s"
                     % (self.sha, self.master_ref))
                self.log.info(c)
                self.add_comment(self.sha, c)
                self.reset_test_ref_to_master()
                self.merge_pull_head_to_test_ref()
                return
            self.log.info("%s - found pending state, checking tests", self.short())
            assert self.merge_sha != None
            bb = BuildBot(self.cfg)
            (t, urls) = bb.test_status(self.merge_sha)
            if t == True:
                self.log.info("%s - tests passed, marking success", self.short())
                c = "all tests pass:\n" + "\n".join(urls)
                self.add_comment(self.sha, c)
                self.set_success("all tests passed")
            elif t == False:
                self.log.info("%s - tests failed, marking failure", self.short())
                c = "some tests failed:\n" + "\n".join(urls)
                self.add_comment(self.sha, c)
                self.set_failure("some tests failed")
            else:
                self.log.info("%s - no info yet, waiting on tests", self.short())

        elif s == STATE_TESTED:
            self.log.info("%s - tests successful, attempting landing", self.short())
            self.advance_master_ref_to_test()



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

    gh = github.GitHub(username=cfg["gh_user"].encode("utf8"),
                       password=cfg["gh_pass"].encode("utf8"))

    owner = cfg["owner"].encode("utf8")
    repo = cfg["repo"].encode("utf8")

    logging.info("loading pull reqs")
    pulls = [ PullReq(cfg, gh, pull) for pull in
              gh.repos(owner)(repo).pulls().get() ]

    #
    # We are reconstructing the relationship between three tree-states on the
    # fly here. We're doing to because there was nowhere useful to leave it
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
    #  master_ref ==>  <master_sha>         <candidate_sha> == p.sha
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
    #   - We get the master_ref's sha, master_sha
    #   - We get the 2 parent links of test_sha
    #   - We exclude the master_sha from that parent list
    #   - Whatever the _other_ parent is, we consider the candidate
    #
    # If we fail to find any steps along the way, bors will either ignore
    # the current state of affairs (i.e. assume it's _not_ presently testing
    # any pull req) or else crash due to inability to load something.
    # So it's non-fatal if we get it wrong; we'll only advance (make changes)
    # if we get it right.
    #

    test_ref = cfg["test_ref"].encode("utf8")
    master_ref = cfg["master_ref"].encode("utf8")
    test_head = gh.repos(owner)(repo).git().refs().heads(test_ref).get()
    master_head = gh.repos(owner)(repo).git().refs().heads(master_ref).get()
    test_sha = test_head["object"]["sha"].encode("utf8")
    master_sha = master_head["object"]["sha"].encode("utf8")
    test_commit = gh.repos(owner)(repo).git().commits(test_sha).get()
    test_parents = [ x["sha"].encode("utf8") for x in test_commit["parents"] ]
    candidate_sha = None
    if len(test_parents) == 2 and master_sha in test_parents:
        test_parents.remove(master_sha)
        candidate_sha = test_parents[0]
        logging.info("test ref '%s' = %.8s, parents: '%s' = %.8s and candidate = %.8s",
                     test_ref, test_sha,
                     master_ref, master_sha,
                     candidate_sha)
    for p in pulls:
        if p.sha == candidate_sha:
            logging.info("candidate = %.8s found in pull req %s",
                         candidate_sha, p.short())
            p.merge_sha = test_sha



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


    pulls = [p for p in pulls if (p.current_state() >= STATE_UNREVIEWED
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
        p.try_advance()



if __name__ == "__main__":
    main()

