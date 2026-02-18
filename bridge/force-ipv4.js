// Force all DNS lookups to return IPv4 addresses only.
// This works around Node.js 22's autoSelectFamily issue in VPCs without IPv6.
const dns = require('dns');
const origLookup = dns.lookup;

dns.lookup = function(hostname, options, callback) {
  if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  if (typeof options === 'number') {
    options = { family: options };
  }
  options = Object.assign({}, options, { family: 4 });
  return origLookup.call(dns, hostname, options, callback);
};
