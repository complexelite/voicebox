# Complex Elite Voicebox deployment

This fork follows upstream Jamie Pine Voicebox and adds a small deployment boundary for the public Complex Elite instance.

## Security configuration

The included `compose.yaml` requires `VOICEBOX_API_KEY` and enables `VOICEBOX_REQUIRE_AUTH`. Set a unique random value of at least 32 characters in Dokploy's secret environment. The private Studio adapter sends it as a Bearer token. For interactive browser access, use HTTP Basic username `voicebox` and the same key as the password; the browser's native credential prompt is used, with no token stored in web storage. Requests from other origins cannot mutate profiles or generate audio. The key must never be committed to Git or put in client-side configuration.

Only the local container health check can call `/health` without authentication. The deployment does not publish the app port directly; Dokploy routes HTTPS traffic to the `voicebox` service on port `17493`.

## Data and models

`voicebox-data-v2` stores profiles, samples and generated output. `voicebox-model-cache` stores downloaded model files. Back up both volumes before replacing or moving the service. The Compose resource limit is sized for the Complex Elite CPU-only VPS; larger models may require additional RAM or a GPU. Download models in Voicebox before selecting them for synthesis.
