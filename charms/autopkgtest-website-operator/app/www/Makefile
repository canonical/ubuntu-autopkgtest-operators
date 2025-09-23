check:
	rm -rf /tmp/autopkgtest-webcontrol-test/
	mkdir /tmp/autopkgtest-webcontrol-test/
	TMPDIR=/tmp/autopkgtest-webcontrol-test/ python3 -m coverage run -m unittest -v
	rm -r /tmp/autopkgtest-webcontrol-test/
	python3 -m coverage report --include="r*" --show-missing --fail-under=100
