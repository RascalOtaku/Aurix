// static/js/integrationActions.js — what removing one Settings integration must actually do.
//
// This mapping used to live inline in settings.js. During the 2026-10-02 api.js migration its
// `if (type === ...) else if (...)` guards were dropped, so deleting one integration cleared every other
// kind too (calendar, contacts, email accounts, MCP servers, vault). Keeping the dispatch here — pure, no
// imports, no DOM — lets tests/test_js_modules.py prove that one type only issues its own request.

function blankCalendar() {
  return { url: '', username: '', password: '' };
}

function blankCardDav() {
  return { carddav_url: '', carddav_username: '', carddav_password: '' };
}

export function integrationRemovalRequests(type, id) {
  const encoded = encodeURIComponent(String(id == null ? '' : id));
  switch (type) {
    case 'api':
      return [{ method: 'DELETE', path: `/api/auth/integrations/${encoded}` }];
    case 'caldav':
      return [{ method: 'POST', path: '/api/calendar/config', body: blankCalendar() }];
    case 'contacts':
      return [{ method: 'DELETE', path: '/api/contacts/clear' }];
    case 'carddav':
      return [{ method: 'PUT', path: '/api/contacts/config', body: blankCardDav() }];
    case 'email':
      return [{ method: 'DELETE', path: `/api/email/accounts/${encoded}` }];
    case 'mcp':
      return [{ method: 'DELETE', path: `/api/mcp/servers/${encoded}` }];
    case 'vault':
      return [{ method: 'POST', path: '/api/vault/logout' }];
    default:
      // An unknown type is a no-op, never "clear everything just in case".
      return [];
  }
}
