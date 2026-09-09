/**
 * GitHub App Webhook → triggers GitHub Actions to deploy to new repo
 *
 * Secrets (set via wrangler secret put):
 *   WEBHOOK_SECRET   - GitHub App webhook secret
 *   PAT              - GitHub PAT with repo scope (to trigger workflow_dispatch)
 *   SOURCE_REPO      - e.g. "laiyinyizao007/my-project-management"
 */

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('OK', { status: 200 });
    }

    const body = await request.text();

    // Verify GitHub webhook signature
    const sig = request.headers.get('x-hub-signature-256');
    if (!sig || !(await verifySignature(body, sig, env.WEBHOOK_SECRET))) {
      return new Response('Unauthorized', { status: 401 });
    }

    const event = request.headers.get('x-github-event');
    if (event !== 'repository') {
      return new Response('Ignored', { status: 200 });
    }

    const payload = JSON.parse(body);
    if (payload.action !== 'created') {
      return new Response('Ignored', { status: 200 });
    }

    const newRepo = payload.repository.full_name;
    console.log(`New repo: ${newRepo} — triggering deploy workflow`);

    // Trigger repository_dispatch on management repo
    ctx.waitUntil(triggerDeploy(newRepo, env));

    return new Response('Accepted', { status: 202 });
  },
};

async function verifySignature(body, signature, secret) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false, ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(body));
  const hex = 'sha256=' + Array.from(new Uint8Array(sig))
    .map(b => b.toString(16).padStart(2, '0')).join('');
  return hex === signature;
}

async function triggerDeploy(targetRepo, env) {
  const [owner, repo] = env.SOURCE_REPO.split('/');
  const res = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.PAT}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'github-repo-autodeploy',
      },
      body: JSON.stringify({
        event_type: 'new-repo-created',
        client_payload: { repo: targetRepo },
      }),
    }
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to trigger deploy: ${res.status} ${text}`);
  }
  console.log(`Deploy triggered for ${targetRepo}`);
}
