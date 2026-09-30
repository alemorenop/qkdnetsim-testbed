AM_CPPFLAGS = \
	-I$(top_srcdir)/src/libstrongswan \
	-I$(top_srcdir)/src/libcharon

AM_CFLAGS = $(PLUGIN_CFLAGS)

if MONOLITHIC
noinst_LTLIBRARIES = libstrongswan-qkd-ppk.la
else
plugin_LTLIBRARIES = libstrongswan-qkd-ppk.la
endif

libstrongswan_qkd_ppk_la_SOURCES = qkd_ppk_plugin.c
libstrongswan_qkd_ppk_la_LDFLAGS = -module -avoid-version
