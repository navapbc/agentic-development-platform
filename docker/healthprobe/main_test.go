package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCheckPassesOnAHealthyLoopbackEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	if err := check(server.URL + "/health"); err != nil {
		t.Fatal(err)
	}
}

func TestCheckFailsOnAnErrorStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()
	if err := check(server.URL); err == nil {
		t.Fatal("expected an error for a 503")
	}
}

func TestCheckRefusesANonLoopbackHost(t *testing.T) {
	for _, raw := range []string{"http://example.com/health", "ftp://127.0.0.1/health", "http://localhost:1/"} {
		if err := check(raw); err == nil {
			t.Fatalf("expected %s to be refused", raw)
		}
	}
}

func tlsServer(t *testing.T) (*httptest.Server, string) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}
	if err := os.WriteFile(caFile, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	return server, caFile
}

func TestCheckVerifiesAnHttpsEndpointAgainstTheCaFile(t *testing.T) {
	server, caFile := tlsServer(t)
	if err := checkWith(server.URL+"/health", caFile, "example.com"); err != nil {
		t.Fatal(err)
	}
}

func TestCheckRefusesHttpsWithoutACaFile(t *testing.T) {
	server, _ := tlsServer(t)
	if err := checkWith(server.URL, "", ""); err == nil {
		t.Fatal("expected an https probe without a CA file to be refused")
	}
}

func TestCheckRefusesACertificateForAnotherName(t *testing.T) {
	server, caFile := tlsServer(t)
	if err := checkWith(server.URL, caFile, "other.example"); err == nil {
		t.Fatal("expected a certificate that does not name other.example to be refused")
	}
}

func TestCheckRefusesACertificateFromAnotherCa(t *testing.T) {
	server, _ := tlsServer(t)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "other CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	otherCa := filepath.Join(t.TempDir(), "other-ca.pem")
	if err := os.WriteFile(otherCa, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := checkWith(server.URL, otherCa, "example.com"); err == nil {
		t.Fatal("expected a certificate from another CA to be refused")
	}
}
