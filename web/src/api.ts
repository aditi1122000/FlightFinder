import type { Cabin, CardPick, RedemptionStrategy, WalletCard } from './types';

export interface PlanRequest {
  originCode: string;
  originCity: string;
  destinationCode: string;
  destinationCity: string;
  depart: string;
  cabin: Cabin;
  mode: 'points' | 'cash' | 'card';
  cardId?: string;
  points?: number;
}

export interface PlanResponse {
  error?: string;
  strategies: RedemptionStrategy[];
  card?: CardPick | null;
}

export async function loadCards(): Promise<WalletCard[]> {
  const response = await fetch('/api/cards');
  if (!response.ok) throw new Error('cards');
  const body = (await response.json()) as { cards?: WalletCard[] };
  return body.cards ?? [];
}

export async function planTrip(request: PlanRequest): Promise<PlanResponse> {
  const response = await fetch('/api/plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  const body = (await response.json()) as PlanResponse;
  if (!response.ok && !body.error) {
    throw new Error('plan');
  }
  return body;
}
