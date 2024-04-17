#!/usr/bin/make -f

DEBPUTY_INSTALLED_ROOT_DIR=/usr/share/dh-debputy
DEBPUTY_INSTALLED_PLUGIN_ROOT_DIR=/usr/share/debputy/

# Nothing to do by default
all:

check:
	py.test -v

install:
	install -m0755 -d \
	    $(DESTDIR)/usr/bin \
	    $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR) \
	    $(DESTDIR)/$(DEBPUTY_INSTALLED_PLUGIN_ROOT_DIR)/debputy \
	    $(DESTDIR)/usr/share/perl5/Debian/Debhelper/Sequence \
	    $(DESTDIR)/usr/share/man/man1
	install -m0755 -t $(DESTDIR)/usr/bin dh_debputy dh_installdebputy assets/debputy
	install -m0755 -t $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR) deb_packer.py deb_materialization.py
	install -m0644 -t $(DESTDIR)/usr/share/perl5/Debian/Debhelper/Sequence lib/Debian/Debhelper/Sequence/*.pm
	cp -a --reflink=auto src/debputy $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR)
	cp -a --reflink=auto debputy $(DESTDIR)/$(DEBPUTY_INSTALLED_PLUGIN_ROOT_DIR)
	sed -i "s/^__version__ =.*/__version__ = '$$(dpkg-parsechangelog -SVersion)'/; s/^__release_commit__ =.*/__release_commit__ = 'N\\/A'/;" \
	   $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR)/debputy/version.py
	perl -p -i -e 's{^DEBPUTY_ROOT_DIR =.*}{DEBPUTY_ROOT_DIR = pathlib.Path("$(DEBPUTY_INSTALLED_ROOT_DIR)")};' \
	   $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR)/debputy/__init__.py
	perl -p -i -e 's{^DEBPUTY_PLUGIN_ROOT_DIR =.*}{DEBPUTY_PLUGIN_ROOT_DIR = pathlib.Path("$(DEBPUTY_INSTALLED_PLUGIN_ROOT_DIR)")};' \
	   $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR)/debputy/__init__.py
	find $(DESTDIR)/usr/share/dh-debputy -type d -name '__pycache__' -exec rm -fr {} +
	pod2man --utf8 --section=1 --name="debputy" -c "The debputy Debian packager helper stack" debputy.pod \
	   $(DESTDIR)/usr/share/man/man1/debputy.1
	pod2man --utf8 --section=1 --name="dh_debputy" -c "The debputy Debian packager helper stack" dh_debputy \
	   $(DESTDIR)/usr/share/man/man1/dh_debputy.1
	pod2man --utf8 --section=1 --name="dh_installdebputy" -c "The debputy Debian packager helper stack" dh_installdebputy \
	   $(DESTDIR)/usr/share/man/man1/dh_installdebputy.1
	chmod -R u=rwX,go=rX \
	    $(DESTDIR)/$(DEBPUTY_INSTALLED_ROOT_DIR)/debputy \
	    $(DESTDIR)/$(DEBPUTY_INSTALLED_PLUGIN_ROOT_DIR)/debputy
