.PHONY: test syntax package clean

test: syntax
	python3 -m unittest discover -s bridge/tests -v
	python3 -m unittest discover -s automation/tests -v

syntax:
	python3 -m py_compile bridge/pressreader_sync_bridge.py bridge/tests/test_bridge.py
	python3 -m py_compile automation/epub_cleaner.py automation/pressreader_worker.py automation/login_server.py automation/tests/test_worker.py automation/tests/test_epub_cleaner.py
	luac -p pressreadersync.koplugin/main.lua
	luac -p pressreadersync.koplugin/client.lua
	sh -n automation/login-entrypoint.sh automation/worker-entrypoint.sh deploy/setup.sh

package:
	mkdir -p dist
	cd . && zip -qr dist/pressreadersync.koplugin.zip pressreadersync.koplugin

clean:
	find bridge automation -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf dist
