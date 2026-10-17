export type Cabin = 'economy' | 'business';

export type Region =
  | 'North America'
  | 'Central America'
  | 'South America'
  | 'Europe'
  | 'Africa'
  | 'Middle East'
  | 'Asia'
  | 'Southeast Asia'
  | 'South Asia'
  | 'Oceania';

export interface TransferProgram {
  id: string;
  name: string;
  issuer: string;
  portalMultiplier: number;
}

export interface AirlinePartner {
  id: string;
  name: string;
  iataCode: string;
  alliance: string;
}

export interface TransferRatio {
  programId: string;
  airlineId: string;
  ratio: number;
  transferTime: string;
}

export interface Card {
  id: string;
  name: string;
  issuer: string;
  network: string;
  annualFee: number;
  pointProgram: string;
  cppEstimate: number;
  perkSummary: string;
  imageColor: string;
  signupBonusPts: number;
}

export interface Destination {
  id: string;
  city: string;
  country: string;
  region: Region;
  airportCode: string;
  imageUrl: string;
  avgCashPriceEconomy: number;
  avgCashPriceBusiness: number;
  description: string;
}

export interface AwardRate {
  airlineId: string;
  region: Region;
  cabin: Cabin;
  milesRequired: number;
  taxesFees: number;
  notes: string;
}

export interface UserCardSelection {
  cardId: string;
  pointsBalance: number;
}

export type RedemptionMethod = 'cash' | 'transfer' | 'portal' | 'points';

export interface RedemptionStrategy {
  id: string;
  method: RedemptionMethod;
  strategyName: string;
  description: string;
  cardName: string;
  airlineName: string;
  pointsUsed: number;
  taxesFees: number;
  totalOutCost: number;
  cashSaved: number;
  valuePerPoint: number;
  transferTime: string;
  badge?: 'best';
  bookingUrl?: string;
  listPrice?: number;
  offerSite?: string;
  offerLabel?: string;
  offerTerms?: string;
  pointProgram?: string;
  termsUrl?: string;
}

export interface WalletCard {
  id: string;
  name: string;
  bank: string;
  currency: string;
  inrPerPoint?: number | null;
}

export interface CardPick {
  name: string;
  bank: string;
  pointProgram: string;
  pitch: string;
  listingUrl?: string;
  termsUrl?: string;
}

export interface OptimizeInput {
  destinationId: string;
  cabin: Cabin;
  cards: UserCardSelection[];
  departDate: string;
  returnDate: string;
}
