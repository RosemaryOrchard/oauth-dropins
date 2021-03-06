"""LinkedIn OAuth drop-in.

API docs:
https://www.linkedin.com/developers/
https://docs.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin
"""
import json
import logging
import urllib

import appengine_config
import handlers
from models import BaseAuth
from webutil import util

from google.appengine.ext import ndb
from webob import exc

# URL templates. Can't (easily) use urlencode() because I want to keep
# the %(...)s placeholders as is and fill them in later in code.
AUTH_CODE_URL = str('&'.join((
    'https://www.linkedin.com/oauth/v2/authorization?'
    'response_type=code',
    'client_id=%(client_id)s',
    # https://docs.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api?context=linkedin/consumer/context#permissions
    'scope=%(scope)s',
    # must be the same in the access token request
    'redirect_uri=%(redirect_uri)s',
    'state=%(state)s',
    )))

ACCESS_TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'
API_PROFILE_URL = 'https://api.linkedin.com/v2/me'


class LinkedInAuth(BaseAuth):
  """An authenticated LinkedIn user.

  Provides methods that return information about this user and make OAuth-signed
  requests to the LinkedIn REST API. Stores OAuth credentials in the datastore.
  See models.BaseAuth for usage details.

  LinkedIn-specific details: TODO
  implements get() but not urlopen(), http(), or api().
  The key name is the ID (a URN).

  Note that LI access tokens can be over 500 chars (up to 1k!), so they need to
  be TextProperty instead of StringProperty.
  https://docs.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow?context=linkedin/consumer/context#access-token-response
  """
  access_token_str = ndb.TextProperty(required=True)
  user_json = ndb.TextProperty()

  def site_name(self):
    return 'LinkedIn'

  def user_display_name(self):
    """Returns the user's first and last name.
    """
    def name(field):
      user = json.loads(self.user_json)
      loc = user.get(field, {}).get('localized', {})
      if loc:
          return loc.get('en_US') or loc.values()[0]
      return ''

    return '%s %s' % (name('firstName'), name('lastName'))

  def access_token(self):
    """Returns the OAuth access token string.
    """
    return self.access_token_str

  def get(self, *args, **kwargs):
    """Wraps requests.get() and adds the Bearer token header.

    TODO: unify with github.py, medium.py.
    """
    return self._requests_call(util.requests_get, *args, **kwargs)

  def post(self, *args, **kwargs):
    """Wraps requests.post() and adds the Bearer token header.

    TODO: unify with github.py, medium.py.
    """
    return self._requests_call(util.requests_post, *args, **kwargs)

  def _requests_call(self, fn, *args, **kwargs):
    headers = kwargs.setdefault('headers', {})
    headers['Authorization'] = 'Bearer ' + self.access_token_str

    resp = fn(*args, **kwargs)
    assert 'serviceErrorCode' not in resp, resp

    try:
      resp.raise_for_status()
    except BaseException, e:
      util.interpret_http_exception(e)
      raise
    return resp


class StartHandler(handlers.StartHandler):
  """Starts LinkedIn auth. Requests an auth code and expects a redirect back.
  """
  DEFAULT_SCOPE = 'r_liteprofile'

  def redirect_url(self, state=None):
    # assert state, 'LinkedIn OAuth 2 requires state parameter'
    assert (appengine_config.LINKEDIN_CLIENT_ID and
            appengine_config.LINKEDIN_CLIENT_SECRET), (
      "Please fill in the linkedin_client_id and "
      "linkedin_client_secret files in your app's root directory.")
    return str(AUTH_CODE_URL % {
      'client_id': appengine_config.LINKEDIN_CLIENT_ID,
      'redirect_uri': urllib.quote_plus(self.to_url()),
      'state': urllib.quote_plus(state or ''),
      'scope': self.scope,
      })


class CallbackHandler(handlers.CallbackHandler):
  """The OAuth callback. Fetches an access token and stores it.
  """
  def get(self):
    # handle errors
    error = self.request.get('error')
    desc = self.request.get('error_description')
    if error:
      # https://docs.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow?context=linkedin/consumer/context#application-is-rejected
      if error in ('user_cancelled_login', 'user_cancelled_authorize'):
        logging.info('User declined: %s', self.request.get('error_description'))
        self.finish(None, state=self.request.get('state'))
        return
      else:
        msg = 'Error: %s: %s' % (error, desc)
        logging.info(msg)
        raise exc.HTTPBadRequest(msg)

    # extract auth code and request access token
    auth_code = util.get_required_param(self, 'code')
    data = {
      'grant_type': 'authorization_code',
      'code': auth_code,
      'client_id': appengine_config.LINKEDIN_CLIENT_ID,
      'client_secret': appengine_config.LINKEDIN_CLIENT_SECRET,
      # redirect_uri here must be the same in the oauth code request!
      # (the value here doesn't actually matter since it's requested server side.)
      'redirect_uri': self.request.path_url,
      }
    resp = util.requests_post(ACCESS_TOKEN_URL, data=urllib.urlencode(data)).json()
    logging.debug('Access token response: %s', resp)
    if resp.get('serviceErrorCode'):
      msg = 'Error: %s' % resp
      logging.info(msg)
      raise exc.HTTPBadRequest(msg)

    access_token = resp['access_token']
    resp = LinkedInAuth(access_token_str=access_token).get(API_PROFILE_URL).json()
    logging.debug('Profile response: %s', resp)
    auth = LinkedInAuth(id=resp['id'], access_token_str=access_token,
                        user_json=json.dumps(resp))
    auth.put()

    self.finish(auth, state=self.request.get('state'))
