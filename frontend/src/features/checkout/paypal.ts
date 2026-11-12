/**
 * The PayPal JavaScript SDK, loaded once on demand with the public client id from the backend.
 * The SDK only collects the buyer's approval; the backend creates, captures and verifies.
 */

export interface PayPalButtons {
  render(container: HTMLElement): Promise<void>;
  close?(): Promise<void>;
}

export interface PayPalButtonOptions {
  style?: Record<string, string>;
  createOrder: () => Promise<string>;
  onApprove: (data: { orderID: string }, actions: { restart: () => Promise<void> }) => Promise<unknown>;
  onCancel?: () => void;
  onError?: (error: unknown) => void;
}

export interface PayPalNamespace {
  Buttons(options: PayPalButtonOptions): PayPalButtons;
}

declare global {
  interface Window {
    paypal?: PayPalNamespace;
  }
}

let loading: Promise<PayPalNamespace> | null = null;

export function loadPayPal(clientId: string): Promise<PayPalNamespace> {
  if (window.paypal) return Promise.resolve(window.paypal);
  loading ??= new Promise<PayPalNamespace>((resolve, reject) => {
    const params = new URLSearchParams({ "client-id": clientId, currency: "USD", intent: "capture", components: "buttons" });
    const script = document.createElement("script");
    script.src = `https://www.paypal.com/sdk/js?${params}`;
    script.async = true;
    script.onload = () => (window.paypal ? resolve(window.paypal) : reject(new Error("PayPal SDK missing")));
    script.onerror = () => {
      loading = null;
      reject(new Error("PayPal SDK failed to load"));
    };
    document.head.appendChild(script);
  });
  return loading;
}
