#!/usr/bin/env python
# -*-coding: utf-8-unix -*-

'''
GitHub API Python SDK. (Python >= 2.5)

Michael Liao (askxuefeng@gmail.com)

Usage:

>>> gh = GitHub(username='githubpy', password='test-githubpy-1234')
>>> L = gh.users('githubpy').followers.get()
>>> L[0].id
470058
>>> L[0].login
u'michaelliao'
>>> x_ratelimit_remaining = gh.x_ratelimit_remaining
>>> x_ratelimit_limit = gh.x_ratelimit_limit
>>> L = gh.users('githubpy').following.get()
>>> L[0]._links.self.href
u'https://api.github.com/users/michaelliao'
>>> L = gh.repos('githubpy')('testgithubpy').issues.get(state='closed', sort='created')
>>> L[0].title
u'sample issue for test'
>>> L[0].number
1
>>> I = gh.repos('githubpy')('testgithubpy').issues(1).get()
>>> I.url
u'https://api.github.com/repos/githubpy/testgithubpy/issues/1'
>>> gh = GitHub(username='githubpy', password='test-githubpy-1234')
>>> r = gh.repos('githubpy')('testgithubpy').issues.post(title='test create issue', body='just a test')
>>> r.title
u'test create issue'
>>> r.state
u'open'
>>> gh.repos.thisisabadurl.get()
Traceback (most recent call last):
    ...
ApiNotFoundError: https://api.github.com/repos/thisisabadurl
>>> gh.users('github-not-exist-user').followers.get()
Traceback (most recent call last):
    ...
ApiNotFoundError: https://api.github.com/users/github-not-exist-user/followers
'''

import re, os, sha, time, hmac, base64, hashlib, urllib, urllib2, mimetypes

try:
    import json
except ImportError:
    import simplejson as json

from collections import Iterable
from datetime import datetime, timedelta, tzinfo
from StringIO import StringIO
TIMEOUT=60

_METHOD_MAP = dict(
        GET=lambda: 'GET',
        PUT=lambda: 'PUT',
        POST=lambda: 'POST',
        PATCH=lambda: 'PATCH',
        DELETE=lambda: 'DELETE')

DEFAULT_SCOPE = None
RW_SCOPE = 'user,public_repo,repo,repo:status,gist'

class GitHub(object):

    '''
    GitHub client.
    '''

    def __init__(self, username=None, password=None, access_token=None, client_id=None, client_secret=None, redirect_uri=None, scope=None, api_url=None):
        if api_url is not None:
            self._URL = api_url
        else:
            self._URL = 'https://api.github.com'
        self.x_ratelimit_remaining = (-1)
        self.x_ratelimit_limit = (-1)
        self._authorization = None
        if username and password:
            self._authorization = 'Basic %s' % base64.b64encode('%s:%s' % (username, password))
        elif access_token:
            self._authorization = 'token %s' % access_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scope = scope

    def authorize_url(self, state=None):
        '''
        Generate authorize_url.

        >>> GitHub(client_id='3ebf94c5776d565bcf75').authorize_url()
        'https://github.com/login/oauth/authorize?client_id=3ebf94c5776d565bcf75'
        '''
        if not self._client_id:
            raise ApiAuthError('No client id.')
        kw = dict(client_id=self._client_id)
        if self._redirect_uri:
            kw['redirect_uri'] = self._redirect_uri
        if self._scope:
            kw['scope'] = self._scope
        if state:
            kw['state'] = state
        return 'https://github.com/login/oauth/authorize?%s' % _encode_params(kw)

    def get_access_token(self, code, state=None):
        '''
        In callback url: http://host/callback?code=123&state=xyz

        use code and state to get an access token.        
        '''
        kw = dict(client_id=self._client_id, client_secret=self._client_secret, code=code)
        if self._redirect_uri:
            kw['redirect_uri'] = self._redirect_uri
        if state:
            kw['state'] = state
        opener = urllib2.build_opener(urllib2.HTTPSHandler)
        request = urllib2.Request('https://github.com/login/oauth/access_token', data=_encode_params(kw))
        request.get_method = _METHOD_MAP['POST']
        request.add_header('Accept', 'application/json')
        try:
            response = opener.open(request, timeout=TIMEOUT)
            r = _parse_json(response.read())
            if 'error' in r:
                raise ApiAuthError(str(r.error))
            return str(r.access_token)
        except urllib2.HTTPError, e:
            raise ApiAuthError('HTTPError when get access token')

    def __getattr__(self, attr):
        return _Callable(self, '/%s' % attr)

    def _http(self, method, path, **kw):
        nretries = 10
        data = None
        params = None
        if method=='GET' and kw:
            path = '%s?%s' % (path, _encode_params(kw))
        if method in ['POST', 'PATCH', 'PUT']:
            data = _encode_json(kw)
        url = '%s%s' % (self._URL, path)
        opener = urllib2.build_opener(urllib2.HTTPSHandler)
        request = urllib2.Request(url, data=data)
        request.get_method = _METHOD_MAP[method]
        if self._authorization:
            request.add_header('Authorization', self._authorization)
        if method in ['POST', 'PATCH', 'PUT']:
            request.add_header('Content-Type', 'application/x-www-form-urlencoded')
        while True:
            try:
                response = opener.open(request, timeout=TIMEOUT)
                is_json = self._process_resp(response.headers)
                if is_json:
                    return _parse_json(response.read())
                if response.code == 204:
                    return None
            except urllib2.HTTPError, e:
                is_json = self._process_resp(e.headers)
                json = None
                if is_json:
                    json = _parse_json(e.read())
                req = JsonObject(method=method, url=url)
                resp = JsonObject(code=e.code, json=json)
                if e.code == 404:
                    raise ApiError(url, req, resp)
                if nretries > 0:
                    nretries -= 1
                    #print "temporary HTTP error, retrying up to %d times..." % nretries
                    print "temporary HTTP error (%d) %s on %s with body %s, retrying up to %d times..." % (e.code, method, path, data, nretries)
                    continue
                if resp.code==404:
                    raise ApiNotFoundError(url, req, resp)
                raise ApiError(url, req, resp)

    def _process_resp(self, headers):
        is_json = False
        if headers:
            for k in headers:
                h = k.lower()
                if h=='x-ratelimit-remaining':
                    self.x_ratelimit_remaining = int(headers[k])
                elif h=='x-ratelimit-limit':
                    self.x_ratelimit_limit = int(headers[k])
                elif h=='content-type':
                    is_json = headers[k].startswith('application/json')
        return is_json

class _Executable(object):

    def __init__(self, gh, method, path):
        self._gh = gh
        self._method = method
        self._path = path

    def __call__(self, **kw):
        return self._gh._http(self._method, self._path, **kw)

    def __str__(self):
        return '_Executable (%s %s)' % (self._method, self._path)

    __repr__ = __str__

class _Callable(object):

    def __init__(self, gh, name):
        self._gh = gh
        self._name = name

    def __call__(self, *args):
        if len(args)==0:
            return self
        name = '%s/%s' % (self._name, '/'.join([str(arg) for arg in args]))
        return _Callable(self._gh, name)

    def __getattr__(self, attr):
        if attr=='get':
            return _Executable(self._gh, 'GET', self._name)
        if attr=='put':
            return _Executable(self._gh, 'PUT', self._name)
        if attr=='post':
            return _Executable(self._gh, 'POST', self._name)
        if attr=='patch':
            return _Executable(self._gh, 'PATCH', self._name)
        if attr=='delete':
            return _Executable(self._gh, 'DELETE', self._name)
        name = '%s/%s' % (self._name, attr)
        return _Callable(self._gh, name)

    def __str__(self):
        return '_Callable (%s)' % self._name

    __repr__ = __str__

def _encode_params(kw):
    '''
    Encode parameters.
    '''
    args = []
    for k, v in kw.iteritems():
        qv = v.encode('utf-8') if isinstance(v, unicode) else str(v)
        args.append('%s=%s' % (k, urllib.quote(qv)))
    return '&'.join(args)

def _encode_json(obj):
    '''
    Encode object as json str.
    '''
    def _dump_obj(obj):
        if isinstance(obj, dict):
            return obj
        d = dict()
        for k in dir(obj):
            if not k.startswith('_'):
                d[k] = getattr(obj, k)
        return d
    return json.dumps(obj, default=_dump_obj)

def _parse_json(jsonstr):
    def _obj_hook(pairs):
        o = JsonObject()
        for k, v in pairs.iteritems():
            o[str(k)] = v
        return o
    return json.loads(jsonstr, object_hook=_obj_hook)

class ApiError(BaseException):

    def __init__(self, url, request, response):
        super(ApiError, self).__init__(url)
        self.request = request
        self.response = response

class ApiAuthError(ApiError):

    def __init__(self, msg):
        super(ApiAuthError, self).__init__(msg, None, None)

class ApiNotFoundError(ApiError):
    pass

class JsonObject(dict):
    '''
    general json object that can bind any fields but also act as a dict.
    '''
    def __getattr__(self, attr):
        return self[attr]

    def __setattr__(self, attr, value):
        self[attr] = value

    def __getstate__(self):
        return self.copy()

    def __setstate__(self, state):
        self.update(state)

if __name__ == '__main__':
    import doctest
    doctest.testmod()
