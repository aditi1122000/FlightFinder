import {
  airlinePartners,
  awardRates,
  cards,
  destinations,
  transferPrograms,
  transferRatios,
} from './data';
import type { OptimizeInput, RedemptionStrategy } from './types';

export function optimizeTrip(input: OptimizeInput): RedemptionStrategy[] {
  const destination = destinations.find((item) => item.id === input.destinationId);
  if (!destination) return [];

  const cashPrice =
    input.cabin === 'business' ? destination.avgCashPriceBusiness : destination.avgCashPriceEconomy;

  const strategies: RedemptionStrategy[] = [
    {
      id: `cash-${destination.id}-${input.cabin}`,
      method: 'cash',
      strategyName: 'Pay cash',
      description: `Average ${input.cabin} cash fare to ${destination.city}.`,
      cardName: 'Any payment card',
      airlineName: 'Any airline',
      pointsUsed: 0,
      taxesFees: 0,
      totalOutCost: cashPrice,
      cashSaved: 0,
      valuePerPoint: 0,
      transferTime: 'Instant',
    },
  ];

  for (const selection of input.cards) {
    const card = cards.find((item) => item.id === selection.cardId);
    if (!card || selection.pointsBalance <= 0) continue;

    const program = transferPrograms.find((item) => item.name === card.pointProgram);
    if (!program) continue;

    const portalCovered = (selection.pointsBalance * (card.cppEstimate / 100)) * program.portalMultiplier;
    const portalOut = Math.max(0, Math.round(cashPrice - portalCovered));
    const portalSaved = Math.max(0, cashPrice - portalOut);
    strategies.push({
      id: `portal-${card.id}-${destination.id}`,
      method: 'portal',
      strategyName: `Book through the ${program.issuer} travel portal`,
      description: `Spend ${selection.pointsBalance.toLocaleString()} ${card.pointProgram} at about ${card.cppEstimate.toFixed(2)}¢ each.`,
      cardName: card.name,
      airlineName: 'Travel portal',
      pointsUsed: selection.pointsBalance,
      taxesFees: portalOut,
      totalOutCost: portalOut,
      cashSaved: portalSaved,
      valuePerPoint: card.cppEstimate,
      transferTime: 'Instant',
    });

    for (const link of transferRatios.filter((item) => item.programId === program.id)) {
      const airline = airlinePartners.find((item) => item.id === link.airlineId);
      const award = awardRates.find(
        (rate) =>
          rate.airlineId === link.airlineId &&
          rate.region === destination.region &&
          rate.cabin === input.cabin,
      );
      if (!airline || !award || link.ratio <= 0) continue;

      const pointsNeeded = Math.ceil(award.milesRequired / link.ratio);
      if (pointsNeeded > selection.pointsBalance) continue;

      const saved = Math.max(0, cashPrice - award.taxesFees);
      const centsPerPoint = (saved / pointsNeeded) * 100;
      strategies.push({
        id: `transfer-${card.id}-${airline.id}-${input.cabin}`,
        method: 'transfer',
        strategyName: `Transfer to ${airline.name}`,
        description: `Move points into ${airline.name} and book the ${award.notes.toLowerCase()} award.`,
        cardName: card.name,
        airlineName: airline.name,
        pointsUsed: pointsNeeded,
        taxesFees: award.taxesFees,
        totalOutCost: award.taxesFees,
        cashSaved: saved,
        valuePerPoint: Math.round(centsPerPoint * 10) / 10,
        transferTime: link.transferTime,
      });
    }
  }

  const best = strategies
    .filter((strategy) => strategy.method !== 'cash' && strategy.cashSaved > 0)
    .sort((a, b) => a.totalOutCost - b.totalOutCost || b.valuePerPoint - a.valuePerPoint)[0];
  if (best) best.badge = 'best';

  const cash = strategies.find((strategy) => strategy.method === 'cash');
  const rest = strategies
    .filter((strategy) => strategy.method !== 'cash')
    .sort((a, b) => a.totalOutCost - b.totalOutCost || b.valuePerPoint - a.valuePerPoint);

  return cash ? [cash, ...rest] : rest;
}
