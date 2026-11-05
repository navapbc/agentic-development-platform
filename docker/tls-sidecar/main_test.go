package main

import (
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestIssueWritesAKeyReadableByTheOwnerOnly(t *testing.T) {
	dir := t.TempDir()
	if err := run([]string{"issue", "-out", dir}); err != nil {
		t.Fatal(err)
	}
	key, err := os.Stat(filepath.Join(dir, keyFile))
	if err != nil {
		t.Fatal(err)
	}
	if key.Mode().Perm() != 0o600 {
		t.Fatalf("key mode %v", key.Mode().Perm())
	}
	certificate, err := tls.LoadX509KeyPair(filepath.Join(dir, certificateFile), filepath.Join(dir, keyFile))
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(certificate.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	if err := leaf.VerifyHostname("127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	if leaf.ExtKeyUsage[0] != x509.ExtKeyUsageServerAuth {
		t.Fatal("the certificate is not for server authentication")
	}
}

func TestAnOwnerThatIsNotNumericIsRefused(t *testing.T) {
	if err := run([]string{"issue", "-out", t.TempDir(), "-owner", "otel"}); err == nil {
		t.Fatal("a non-numeric owner was accepted")
	}
}

func TestAnUpstreamThatIsNotPlainHTTPIsRefused(t *testing.T) {
	for _, upstream := range []string{"", "https://127.0.0.1:3000", "127.0.0.1:3000"} {
		if err := run([]string{"proxy", "-upstream", upstream}); err == nil {
			t.Fatalf("upstream %q was accepted", upstream)
		}
	}
}

func startProxy(t *testing.T, upstream *httptest.Server) (string, *x509.CertPool) {
	t.Helper()
	target, _ := url.Parse(upstream.URL)
	issued, err := issue(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	server, err := newServer("127.0.0.1:0", target, issued)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go server.ServeTLS(listener, "", "")
	t.Cleanup(func() { server.Close() })
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(issued.certificatePEM)
	return "https://" + listener.Addr().String(), pool
}

func TestTheProxyForwardsOverTLSWithTheOriginalHost(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Seen", r.Host+" "+r.Header.Get("X-Forwarded-Proto")+" "+r.Header.Get("X-Forwarded-For"))
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	address, pool := startProxy(t, upstream)

	request, _ := http.NewRequest("GET", address+"/api/public/health", nil)
	request.Host = "langfuse.platform.internal"
	request.Header.Set("X-Forwarded-For", "10.0.1.5")
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if seen := response.Header.Get("X-Seen"); seen != "langfuse.platform.internal https 10.0.1.5, 127.0.0.1" {
		t.Fatalf("upstream saw %q", seen)
	}
}

func TestTheProxyRefusesTLSOlderThan12(t *testing.T) {
	issued, err := issue(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	server, err := newServer(":0", &url.URL{Scheme: "http", Host: "127.0.0.1:3000"}, issued)
	if err != nil {
		t.Fatal(err)
	}
	if server.TLSConfig.MinVersion != tls.VersionTLS12 {
		t.Fatalf("minimum TLS version %x", server.TLSConfig.MinVersion)
	}
}
