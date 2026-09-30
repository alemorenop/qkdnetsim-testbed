/*
 * Dynamic RFC 8784 PPK credential provider for strongSwan 5.9.x.
 *
 * The plugin deliberately knows nothing about ETSI QKD APIs.  It forwards the
 * PPK identity selected by IKEv2 to a local Unix socket.  The endpoint process
 * resolves that identity against its local QKD KMS and returns the key bytes.
 * Keeping the ETSI client outside charon makes the provider independent of the
 * selected ETSI interface and keeps all network access local to the endpoint.
 */

#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <library.h>
#include <plugins/plugin.h>
#include <plugins/plugin_feature.h>
#include <credentials/credential_set.h>
#include <credentials/keys/shared_key.h>

#define DEFAULT_SOCKET_PATH "/run/qkd-vpn/ppk-provider.sock"
#define DEFAULT_TIMEOUT_SECONDS 15
#define MAX_IDENTITY_BYTES 1024
#define MAX_RESPONSE_BYTES 8192

typedef struct private_qkd_ppk_plugin_t private_qkd_ppk_plugin_t;
typedef struct private_qkd_ppk_credentials_t private_qkd_ppk_credentials_t;

struct private_qkd_ppk_credentials_t
{
	credential_set_t public;
	private_qkd_ppk_plugin_t *plugin;
};

struct private_qkd_ppk_plugin_t
{
	plugin_t public;
	private_qkd_ppk_credentials_t credentials;
	bool credentials_registered;
	char *socket_path;
	int timeout_seconds;
};

static bool write_all(int fd, const char *buffer, size_t length)
{
	while (length)
	{
		ssize_t written = write(fd, buffer, length);
		if (written < 0)
		{
			if (errno == EINTR)
			{
				continue;
			}
			return FALSE;
		}
		buffer += written;
		length -= written;
	}
	return TRUE;
}

static bool hex_to_chunk(const char *hex, chunk_t *secret)
{
	size_t length = strlen(hex), i;

	if (!length || length % 2)
	{
		return FALSE;
	}
	*secret = chunk_alloc(length / 2);
	for (i = 0; i < secret->len; i++)
	{
		unsigned int value;
		if (sscanf(hex + (i * 2), "%2x", &value) != 1)
		{
			chunk_clear(secret);
			return FALSE;
		}
		secret->ptr[i] = value;
	}
	return TRUE;
}

static shared_key_t *resolve_ppk(private_qkd_ppk_plugin_t *this,
							 identification_t *identity)
{
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	struct timeval timeout;
	chunk_t encoding, secret = chunk_empty;
	char request[5 + MAX_IDENTITY_BYTES * 2 + 2];
	char response[MAX_RESPONSE_BYTES];
	size_t offset, i;
	int fd;

	encoding = identity->get_encoding(identity);
	if (!encoding.len || encoding.len > MAX_IDENTITY_BYTES)
	{
		DBG1(DBG_CFG, "QKD PPK identity has invalid length: %zu", encoding.len);
		return NULL;
	}
	if (strlen(this->socket_path) >= sizeof(address.sun_path))
	{
		DBG1(DBG_CFG, "QKD PPK provider socket path is too long");
		return NULL;
	}

	memcpy(request, "GET ", 4);
	for (i = 0; i < encoding.len; i++)
	{
		snprintf(request + 4 + i * 2, 3, "%02x", encoding.ptr[i]);
	}
	offset = 4 + encoding.len * 2;
	request[offset++] = '\n';
	request[offset] = '\0';

	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
	{
		DBG1(DBG_CFG, "creating QKD PPK provider socket failed: %s",
			 strerror(errno));
		return NULL;
	}
	timeout.tv_sec = this->timeout_seconds;
	timeout.tv_usec = 0;
	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
	setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
	strncpy(address.sun_path, this->socket_path, sizeof(address.sun_path) - 1);

	if (connect(fd, (struct sockaddr*)&address, sizeof(address)) < 0 ||
		!write_all(fd, request, offset))
	{
		DBG1(DBG_CFG, "connecting to QKD PPK provider '%s' failed: %s",
			 this->socket_path, strerror(errno));
		close(fd);
		return NULL;
	}

	offset = 0;
	while (offset + 1 < sizeof(response))
	{
		ssize_t received = read(fd, response + offset, 1);
		if (received == 1)
		{
			if (response[offset++] == '\n')
			{
				break;
			}
			continue;
		}
		if (received < 0 && errno == EINTR)
		{
			continue;
		}
		break;
	}
	close(fd);
	response[offset] = '\0';
	if (offset && response[offset - 1] == '\n')
	{
		response[--offset] = '\0';
	}
	if (offset && response[offset - 1] == '\r')
	{
		response[--offset] = '\0';
	}
	if (strncmp(response, "OK ", 3) != 0 ||
		!hex_to_chunk(response + 3, &secret))
	{
		DBG1(DBG_CFG, "QKD PPK provider rejected PPK_ID '%Y': %s",
			 identity, response[0] ? response : "empty response");
		return NULL;
	}
	DBG1(DBG_CFG, "resolved RFC 8784 PPK_ID '%Y' through local QKD provider",
		 identity);
	return shared_key_create(SHARED_PPK, secret);
}

CALLBACK(shared_filter, bool,
	void *unused, enumerator_t *original, va_list args)
{
	shared_key_t *key, **out;
	id_match_t *match_me, *match_other;

	VA_ARGS_VGET(args, out, match_me, match_other);
	if (!original->enumerate(original, &key))
	{
		return FALSE;
	}
	*out = key;
	if (match_me)
	{
		*match_me = ID_MATCH_PERFECT;
	}
	if (match_other)
	{
		*match_other = ID_MATCH_ANY;
	}
	return TRUE;
}

METHOD(credential_set_t, create_shared_enumerator, enumerator_t*,
	private_qkd_ppk_credentials_t *this, shared_key_type_t type,
	identification_t *me, identification_t *other)
{
	shared_key_t *key;

	if (type != SHARED_PPK && type != SHARED_ANY)
	{
		return NULL;
	}
	if (!me)
	{
		/* The responder enumerates without an identity during IKE_SA_INIT only
		 * to decide whether it supports USE_PPK.  The value is never used. */
		chunk_t advertised = chunk_alloc(32);
		memset(advertised.ptr, 0, advertised.len);
		key = shared_key_create(SHARED_PPK, advertised);
	}
	else
	{
		key = resolve_ppk(this->plugin, me);
		if (!key)
		{
			return NULL;
		}
	}

	/* The enumerator owns one dynamically resolved candidate and yields it once. */
	return enumerator_create_filter(
		enumerator_create_single(key, (void*)key->destroy),
		shared_filter, NULL, NULL);
}

static bool register_provider(private_qkd_ppk_plugin_t *this,
							  plugin_feature_t *feature, bool reg, void *data)
{
	if (reg)
	{
		lib->credmgr->add_set(lib->credmgr, &this->credentials.public);
		this->credentials_registered = TRUE;
	}
	else if (this->credentials_registered)
	{
		lib->credmgr->remove_set(lib->credmgr, &this->credentials.public);
		this->credentials_registered = FALSE;
	}
	return TRUE;
}

METHOD(plugin_t, get_name, char*, private_qkd_ppk_plugin_t *this)
{
	return "qkd-ppk";
}

METHOD(plugin_t, get_features, int, private_qkd_ppk_plugin_t *this,
	   plugin_feature_t *features[])
{
	static plugin_feature_t available[] = {
		PLUGIN_CALLBACK((plugin_feature_callback_t)register_provider, NULL),
			PLUGIN_PROVIDE(CUSTOM, "qkd-ppk"),
	};
	*features = available;
	return countof(available);
}

METHOD(plugin_t, destroy, void, private_qkd_ppk_plugin_t *this)
{
	free(this->socket_path);
	free(this);
}

plugin_t *qkd_ppk_plugin_create(void)
{
	private_qkd_ppk_plugin_t *this;
	char *socket_path;

	socket_path = lib->settings->get_str(lib->settings,
		"%s.plugins.qkd-ppk.socket", DEFAULT_SOCKET_PATH, lib->ns);
	INIT(this,
		.public = {
			.get_name = _get_name,
			.get_features = _get_features,
			.reload = (void*)return_false,
			.destroy = _destroy,
		},
		.credentials = {
			.public = {
				.create_private_enumerator = (void*)return_null,
				.create_cert_enumerator = (void*)return_null,
				.create_shared_enumerator = _create_shared_enumerator,
				.create_cdp_enumerator = (void*)return_null,
				.cache_cert = (void*)nop,
			},
		},
		.socket_path = strdup(socket_path),
		.timeout_seconds = lib->settings->get_int(lib->settings,
			"%s.plugins.qkd-ppk.timeout", DEFAULT_TIMEOUT_SECONDS, lib->ns),
	);
	this->credentials.plugin = this;
	return &this->public;
}
