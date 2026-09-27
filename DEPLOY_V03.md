# DAiL v0.3 deployment checklist

1. Upload this release to the `main` branch of the DAiL GitHub repository.
2. Let Render redeploy from the updated repository.
3. In Render, add an environment variable named `DAIL_ADMIN_KEY` with a long random private value.
4. Do not put `DAIL_ADMIN_KEY` in GitHub, the README, agent prompts, browser JavaScript, or Observatory output.
5. Open `/health`, `/safe`, and `/docs` after deployment.
6. Use `/safe/keys/withdrawal` once with the admin key to create the withdrawal key. Store the returned withdrawal key in a password manager or secrets manager.
7. Revoke and rotate the withdrawal key if it is ever exposed.

### What the current release does NOT do

- It does not move real dollars.
- It does not connect to Stripe, Apple Pay, a bank, or a blockchain.
- It does not provide regulated custody.
- Safe state is in memory and resets when the server restarts.

Those pieces should be added only after the test safe passes its security and persistence review.
