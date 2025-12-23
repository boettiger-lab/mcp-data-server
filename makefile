.ONESHELL:

.PHONY: publish

publish:
	uv publish --token $(PYPI_KEY)
