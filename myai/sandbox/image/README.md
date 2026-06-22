# myai pi sandbox image

Custom Gondolin guest image with Node.js and pi preinstalled.

Generate a default Gondolin build config, then customize packages:

```bash
npx @earendil-works/gondolin build --init-config > build-config.json
```

Add to the rootfs packages in `build-config.json`:

- `nodejs` (or install Node via image init script)
- `npm`
- `ripgrep` (pi grep/find tools)
- `bash`, `git`, `ca-certificates`

After build, tag and point myai at it in `.myai/sandbox.json`:

```json
{
  "image": "myai-pi:latest",
  "install_pi_at_boot": false
}
```

Build and import:

```bash
npx @earendil-works/gondolin build --config build-config.json --tag myai-pi:latest
```

Bake pi into the image init script (example snippet for build pipeline):

```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
```

Set `install_pi_at_boot: false` once pi is baked in.
