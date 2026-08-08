FROM python:3.13-slim
WORKDIR /organism
COPY seed/smolagents /opt/smolagents
RUN pip install --no-cache-dir "/opt/smolagents[litellm,mcp]"
RUN mkdir -p /opt/kadath && python -c "import hashlib,pathlib; root=pathlib.Path('/opt/smolagents'); h=hashlib.sha256(); [(h.update(name.encode()),h.update((root/name).read_bytes())) for name in ('pyproject.toml','requirements.txt') if (root/name).is_file()]; pathlib.Path('/opt/kadath/base-dependencies.sha256').write_text(h.hexdigest())"
COPY seed/organism.py /organism/organism.py
COPY seed/kadath_runtime.py /organism/kadath_runtime.py
COPY seed/container_entrypoint.py /opt/kadath/container_entrypoint.py
ENTRYPOINT ["python", "/opt/kadath/container_entrypoint.py"]
CMD ["python", "/organism/organism.py"]
