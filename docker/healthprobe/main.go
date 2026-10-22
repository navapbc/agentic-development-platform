// Command healthprobe is a static HTTP health check for images that ship no shell or HTTP
// client. It exits 0 when a GET to the loopback URL in its first argument answers below 400.
package main

import (
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"time"
)

func check(raw string) error {
	target, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if target.Scheme != "http" {
		return fmt.Errorf("unsupported scheme %q", target.Scheme)
	}
	ip := net.ParseIP(target.Hostname())
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("host %q is not a loopback address", target.Hostname())
	}
	client := &http.Client{
		Timeout: 5 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Get(target.String())
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		return fmt.Errorf("status %d", response.StatusCode)
	}
	return nil
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: healthprobe http://127.0.0.1:PORT/PATH")
		os.Exit(2)
	}
	if err := check(os.Args[1]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
