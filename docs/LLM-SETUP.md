**LLM Setup**

- **Goal**: keep large model files out of the EE container image. The container provides a small runtime (`llama.cpp`) and expects model files to live on the host filesystem, mounted into the container at runtime.

- **Host install**: run the helper to build `llama.cpp` on the host (requires sudo):

```bash
sudo ./scripts/install_llamacpp.sh /var/lib/mcp-llms /opt/llama.cpp
```

- **Download models**: use the downloader to fetch a model file to the host storage (example):

```bash
./scripts/llm_download_model.sh https://example.com/path/to/model.bin /var/lib/mcp-llms
```

- **Run container with model mount**: when starting the EE container (podman/docker), bind the model directory into the container, e.g.:

```bash
podman run --rm -it -v /var/lib/mcp-llms:/var/lib/mcp-llms mcp-ee:latest /bin/bash
```

Inside the container use the `llama-cpp-run` wrapper (if the runtime was built into the image) or use the host-installed binary. Models should be referenced using the mounted path `/var/lib/mcp-llms/<model-file>`.

- **Ollama note**: If you prefer `ollama` (a separate runtime), install it on the host and expose its model path into the container similarly. This repository will not bake full model files into the image by design.
