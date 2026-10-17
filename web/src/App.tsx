import { useEffect, useState, type ReactNode } from 'react';
import {
  ArrowRight,
  ChevronDown,
  CreditCard,
  ExternalLink,
  Info,
  MapPin,
  Plane,
  RotateCcw,
  Sparkles,
  Wallet,
  X,
} from 'lucide-react';
import type { Cabin, CardPick, Destination, RedemptionStrategy, WalletCard } from './types';
import { destinations } from './data';
import { loadCards, planTrip } from './api';

type Screen = 'start' | 'points' | 'results';
type TravelerMode = 'points' | 'cash' | 'card';

const COVERS = [
  'https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=2000&q=80',
  'https://images.unsplash.com/photo-1509316785289-025f5b846b35?auto=format&fit=crop&w=2000&q=80',
  'https://images.unsplash.com/photo-1469474968028-56623f02e42e?auto=format&fit=crop&w=2000&q=80',
  'https://images.unsplash.com/photo-1499346030926-9a72daac6c63?auto=format&fit=crop&w=2000&q=80',
];

function anotherCover(current?: string) {
  const pool = COVERS.filter((url) => url !== current);
  return pool[Math.floor(Math.random() * pool.length)];
}

export default function App() {
  const [screen, setScreen] = useState<Screen>('start');
  const [cover, setCover] = useState(anotherCover);
  const [tripText, setTripText] = useState('');
  const [tripNote, setTripNote] = useState('');
  const [origin, setOrigin] = useState<Destination>();
  const [selectedDestination, setSelectedDestination] = useState<Destination>();
  const [cabin, setCabin] = useState<Cabin>('economy');
  const [departDate, setDepartDate] = useState('');
  const [wallet, setWallet] = useState<WalletCard[]>([]);
  const [walletError, setWalletError] = useState(false);
  const [cardId, setCardId] = useState('');
  const [pointsBalance, setPointsBalance] = useState('0');
  const [travelerMode, setTravelerMode] = useState<TravelerMode>('points');
  const [results, setResults] = useState<RedemptionStrategy[]>([]);
  const [cardPick, setCardPick] = useState<CardPick | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    loadCards()
      .then((cards) => {
        setWallet(cards);
        setCardId((current) => current || cards[0]?.id || '');
      })
      .catch(() => setWalletError(true));
  }, []);

  const goToPoints = () => {
    const trip = parseTrip(tripText);
    if (!trip) {
      setTripNote('Try a line like Delhi to Singapore on 20 November.');
      return;
    }
    setTripNote('');
    setOrigin(trip.origin);
    setSelectedDestination(trip.destination);
    setDepartDate(trip.depart);
    setCabin(trip.cabin);
    setScreen('points');
  };

  const findDeal = async () => {
    if (!origin || !selectedDestination || !departDate || busy) return;
    setBusy(true);
    setError('');
    try {
      const plan = await planTrip({
        originCode: origin.airportCode,
        originCity: origin.city,
        destinationCode: selectedDestination.airportCode,
        destinationCity: selectedDestination.city,
        depart: departDate,
        cabin,
        mode: travelerMode,
        cardId,
        points: Number(pointsBalance) || 0,
      });
      if (plan.error) {
        setError(plan.error);
        return;
      }
      setResults(plan.strategies);
      setCardPick(plan.card ?? null);
      setScreen('results');
    } catch {
      setError('The fare search did not answer. Try that route again.');
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setScreen('start');
    setCover((current) => anotherCover(current));
    setTripText('');
    setTripNote('');
    setOrigin(undefined);
    setSelectedDestination(undefined);
    setCabin('economy');
    setDepartDate('');
    setCardId(wallet[0]?.id ?? '');
    setPointsBalance('0');
    setTravelerMode('points');
    setResults([]);
    setCardPick(null);
    setError('');
  };

  return (
    <div className="min-h-screen bg-[#f4f5f2] text-[#202522]">
      {screen === 'start' && (
        <Landing tripText={tripText} setTripText={setTripText} note={tripNote} cover={cover} onContinue={goToPoints} />
      )}
      {screen === 'points' && (
        <PointsStep
          destination={selectedDestination}
          cabin={cabin}
          departDate={departDate}
          wallet={wallet}
          walletError={walletError}
          cardId={cardId}
          setCardId={setCardId}
          pointsBalance={pointsBalance}
          setPointsBalance={setPointsBalance}
          mode={travelerMode}
          setMode={setTravelerMode}
          busy={busy}
          error={error}
          onBack={reset}
          onFind={findDeal}
        />
      )}
      {screen === 'results' && (
        <Results
          destination={selectedDestination}
          cabin={cabin}
          departDate={departDate}
          results={results}
          mode={travelerMode}
          cardPick={cardPick}
          onReset={reset}
        />
      )}
    </div>
  );
}

function Header({ onReset, dark = false }: { onReset?: () => void; dark?: boolean }) {
  return (
    <header className={`absolute inset-x-0 top-0 z-20 ${dark ? 'text-white' : 'text-[#202522]'}`}>
      <div className="mx-auto flex h-[76px] max-w-7xl items-center justify-between px-6 lg:px-10">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#f6bd19] text-[#202522] shadow-lg shadow-black/10">
            <Plane className="h-4 w-4" />
          </div>
          <span className="text-[15px] font-bold tracking-tight">GarudaX</span>
          <span className="rounded-full bg-[#f6bd19] px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-[#202522]">Beta</span>
        </div>
        {onReset && (
          <button onClick={onReset} className="flex h-9 items-center gap-2 rounded-full border border-current/20 bg-white/10 px-4 text-xs font-semibold backdrop-blur transition hover:bg-white/20">
            <RotateCcw className="h-3.5 w-3.5" /> New trip
          </button>
        )}
      </div>
    </header>
  );
}

function Landing({ tripText, setTripText, note, cover, onContinue }: {
  tripText: string;
  setTripText: (value: string) => void;
  note: string;
  cover: string;
  onContinue: () => void;
}) {
  const ready = Boolean(parseTrip(tripText));
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[#1b1814] font-['Manrope','Avenir_Next',sans-serif] text-white">
      <div
        className="absolute inset-[-12%] scale-[1.035] bg-cover bg-[center_42%]"
        style={{ backgroundImage: `url("${cover}")` }}
      />
      <div className="absolute inset-0 bg-[linear-gradient(180deg,rgba(8,6,4,0.58),rgba(8,6,4,0.46)_40%,rgba(8,6,4,0.62))]" />
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_76%_30%_at_50%_44%,rgba(6,4,2,0.82),rgba(6,4,2,0.4)_52%,rgba(6,4,2,0)_78%)]" />
      <div className="relative z-10 flex w-full max-w-[980px] flex-col items-center px-5 pb-[7vh] text-center">
        <p className="mb-5 rounded-full border border-white/30 bg-black/20 px-3 py-1.5 text-[0.72rem] font-bold uppercase tracking-[0.22em] text-white">GarudaX</p>
        <h1 className="text-[clamp(3rem,5.8vw,4.6rem)] font-extrabold leading-[1.02] tracking-[-0.05em] text-white">Tell us where you're going.</h1>
        <p className="mt-2 text-[clamp(1.45rem,2.6vw,2.05rem)] font-bold leading-[1.12] tracking-[-0.04em] text-[#F5C400]">We'll find what you actually pay.</p>
        <p className="mt-4 text-[0.98rem] font-medium text-white/80">
          The cash fare<span className="mx-2.5 text-white/45">·</span>Your card offer<span className="mx-2.5 text-white/45">·</span>Your points
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (ready) onContinue();
          }}
          className="relative mx-auto mt-7 h-12 w-full max-w-[560px]"
        >
          <input
            value={tripText}
            onChange={(event) => setTripText(event.target.value)}
            placeholder="Delhi to Singapore on 20 November"
            aria-label="Your trip"
            className="ml-[62px] h-12 w-[calc(100%-124px)] border-b border-white/40 bg-transparent text-center text-[0.98rem] font-medium text-white/70 outline-none placeholder:text-white/50"
          />
          <button
            type="submit"
            disabled={!ready}
            aria-label="Continue"
            className="absolute right-0 top-1/2 flex h-[46px] w-[46px] -translate-y-1/2 items-center justify-center disabled:opacity-35"
          >
            <span className="block h-[9px] w-[9px] -translate-x-[8%] translate-y-[-8%] rotate-45 border-r-[1.5px] border-t-[1.5px] border-white/90" />
          </button>
        </form>
        {note && <p className="mt-4 text-sm text-white">{note}</p>}
      </div>
    </main>
  );
}

function PointsStep({
  destination, cabin, departDate, wallet, walletError, cardId, setCardId, pointsBalance, setPointsBalance, mode, setMode, busy, error, onBack, onFind,
}: {
  destination?: Destination;
  cabin: Cabin;
  departDate: string;
  wallet: WalletCard[];
  walletError: boolean;
  cardId: string;
  setCardId: (value: string) => void;
  pointsBalance: string;
  setPointsBalance: (value: string) => void;
  mode: TravelerMode;
  setMode: (value: TravelerMode) => void;
  busy: boolean;
  error: string;
  onBack: () => void;
  onFind: () => void;
}) {
  const banks = [...new Set(wallet.map((card) => card.bank))];
  const needsPoints = mode === 'points' && (Number(pointsBalance) <= 0 || !cardId);
  return (
    <div className="min-h-screen bg-[#f4f5f2]">
      <Header onReset={onBack} />
      <div className="mx-auto grid max-w-6xl items-center gap-12 px-6 pb-16 pt-28 lg:grid-cols-[minmax(0,.8fr)_minmax(0,1.2fr)]">
        <div>
          <span className="text-xs font-bold uppercase tracking-[.18em] text-[#79837d]">Step 2 of 2</span>
          <h1 className="mt-4 text-5xl font-semibold leading-[1.02] tracking-[-.05em] text-[#202522]">
            Tell me how<br /><span className="text-[#d29d00]">you want to pay.</span>
          </h1>
          <p className="mt-5 max-w-md text-base leading-7 text-[#68716c]">I’ll show the cash fare either way, then compare points or recommend a card if that could lower the price.</p>
          <div className="mt-10 rounded-2xl bg-[#e9ede8] p-5">
            <p className="text-xs font-semibold uppercase tracking-wider text-[#7a847e]">Your trip</p>
            <div className="mt-3 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-white text-[#47705e]"><MapPin className="h-4 w-4" /></div>
              <div>
                <p className="font-semibold text-[#202522]">{destination?.city}</p>
                <p className="text-xs text-[#7a847e]">{cabin === 'business' ? 'Business class' : 'Economy'} · {formatMonth(departDate)}</p>
              </div>
            </div>
          </div>
        </div>
        <div className="rounded-[28px] bg-white p-6 shadow-xl shadow-[#273b3012] md:p-8">
          <div className="mb-6 grid grid-cols-3 gap-1 rounded-xl bg-[#f1f3ef] p-1">
            <ModeButton active={mode === 'points'} onClick={() => setMode('points')}>I have points</ModeButton>
            <ModeButton active={mode === 'cash'} onClick={() => setMode('cash')}>I’ll pay cash</ModeButton>
            <ModeButton active={mode === 'card'} onClick={() => setMode('card')}>Find a card</ModeButton>
          </div>
          {mode === 'cash' ? (
            <ModeNote icon={<Wallet className="h-4 w-4" />} title="Show me the cash fare" body="I’ll pull the live fare for this route and keep that as your baseline." />
          ) : mode === 'card' ? (
            <ModeNote icon={<Sparkles className="h-4 w-4" />} title="Find the best card for this trip" body="I’ll show the cash fare first, then a card whose points or offer could make this route cheaper." />
          ) : (
            <>
              <div className="mb-5 flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-[#eaf1eb] text-[#47705e]"><CreditCard className="h-5 w-5" /></div>
                <div>
                  <h2 className="text-lg font-semibold">Your points balance</h2>
                  <p className="text-sm text-[#89918b]">Choose the card that holds your travel points.</p>
                </div>
              </div>
              <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-[#737c76]">Credit card</label>
              <div className="relative">
                <select value={cardId} onChange={(event) => setCardId(event.target.value)} className="h-14 w-full appearance-none rounded-xl border border-[#dfe4df] bg-[#fafbf9] px-4 pr-10 text-sm font-medium outline-none focus:border-[#47705e]">
                  {banks.map((bank) => (
                    <optgroup key={bank} label={bank}>
                      {wallet.filter((card) => card.bank === bank).map((card) => (
                        <option key={card.id} value={card.id}>{cardOption(card)}</option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-4 top-1/2 h-4 w-4 -translate-y-1/2 text-[#7d867f]" />
              </div>
              <label className="mb-2 mt-6 block text-xs font-bold uppercase tracking-wider text-[#737c76]">Available points</label>
              <div className="relative">
                <Wallet className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-[#7d867f]" />
                <input type="number" min="1" value={pointsBalance} onChange={(event) => setPointsBalance(event.target.value)} className="h-14 w-full rounded-xl border border-[#dfe4df] bg-[#fafbf9] pl-11 pr-4 text-lg font-semibold tabular-nums outline-none focus:border-[#47705e]" />
              </div>
              {walletError && <p className="mt-3 text-sm text-[#9a5b2f]">The card list did not load. Start the fare service and try again.</p>}
            </>
          )}
          {error && <p className="mt-4 rounded-xl bg-[#fff8e7] px-4 py-3 text-sm text-[#7b6a3a]">{error}</p>}
          <button onClick={onFind} disabled={busy || needsPoints} className="mt-8 flex h-14 w-full items-center justify-center gap-2 rounded-xl bg-[#202a2c] text-sm font-semibold text-white transition hover:bg-[#334447] disabled:opacity-40">
            {busy ? 'Searching fares…' : mode === 'cash' ? 'Show cash fares' : mode === 'card' ? 'Show card opportunities' : 'Compare my points'}
            {!busy && <ArrowRight className="h-4 w-4" />}
          </button>
          <p className="mt-4 flex items-center justify-center gap-2 text-center text-xs text-[#89918b]"><Info className="h-3.5 w-3.5" /> Estimates only. Always verify availability.</p>
        </div>
      </div>
    </div>
  );
}

function ModeNote({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return (
    <div className="rounded-2xl bg-[#f8faf7] p-5">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-white text-[#47705e]">{icon}</div>
        <div>
          <h2 className="font-semibold">{title}</h2>
          <p className="mt-1 text-sm leading-6 text-[#89918b]">{body}</p>
        </div>
      </div>
    </div>
  );
}

function ModeButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return <button onClick={onClick} className={`rounded-lg px-2 py-2.5 text-center text-[13px] font-semibold leading-4 transition ${active ? 'bg-white text-[#202522] shadow-sm' : 'text-[#7c867e] hover:text-[#202522]'}`}>{children}</button>;
}

function Results({
  destination, cabin, departDate, results, mode, cardPick, onReset,
}: {
  destination?: Destination;
  cabin: Cabin;
  departDate: string;
  results: RedemptionStrategy[];
  mode: TravelerMode;
  cardPick: CardPick | null;
  onReset: () => void;
}) {
  const [bookingStrategy, setBookingStrategy] = useState<RedemptionStrategy>();
  const best = results.find((result) => result.badge === 'best');
  const cash = results.find((result) => result.method === 'cash');
  return (
    <div className="min-h-screen bg-[#f4f5f2]">
      <Header onReset={onReset} />
      <div className="mx-auto grid max-w-7xl gap-0 px-4 pb-12 pt-24 lg:grid-cols-[290px_minmax(0,1fr)] lg:px-8">
        <aside className="relative min-h-[280px] overflow-hidden rounded-t-[26px] lg:min-h-[calc(100vh-128px)] lg:rounded-[26px]">
          <img src={destination?.imageUrl} alt={destination?.city} className="absolute inset-0 h-full w-full object-cover" />
          <div className="absolute inset-0 bg-gradient-to-t from-[#12201c]/95 via-[#12201c]/30 to-transparent" />
          <div className="relative flex h-full min-h-[280px] flex-col justify-end p-6 text-white lg:min-h-[calc(100vh-128px)]">
            <span className="text-[10px] font-bold uppercase tracking-[.2em] text-white/55">Your journey</span>
            <h1 className="mt-3 text-4xl font-semibold tracking-[-.04em]">{destination?.city}.</h1>
            <p className="mt-2 text-sm text-white/65">{cabin === 'business' ? 'Business class' : 'Economy'} · {formatMonth(departDate)}</p>
            <div className="mt-8 inline-flex w-fit items-center gap-2 rounded-full border border-white/15 bg-white/10 px-3 py-2 text-xs backdrop-blur">
              <Sparkles className="h-3.5 w-3.5 text-[#f6bd19]" /> {mode === 'cash' ? 'Cash fare view' : 'Personalized estimate'}
            </div>
          </div>
        </aside>
        <section className="rounded-b-[26px] bg-white px-5 py-7 md:px-10 md:py-9 lg:rounded-r-[26px] lg:rounded-bl-none">
          <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="text-xs font-bold uppercase tracking-[.18em] text-[#7d867f]">Your answer</p>
              <h2 className="mt-2 text-3xl font-semibold tracking-[-.04em]">What you’ll actually pay</h2>
              <p className="mt-2 text-sm text-[#778079]">{mode === 'cash' ? 'Your cash baseline, with a direct booking path.' : 'Cash baseline first, then the smartest point strategy.'}</p>
            </div>
            {best && (
              <div className="rounded-2xl bg-[#edf6ed] px-5 py-3 text-right">
                <p className="text-[10px] font-bold uppercase tracking-wider text-[#5c8165]">Best option saves</p>
                <p className="mt-1 text-2xl font-bold tabular-nums text-[#287447]">{inr(best.cashSaved)}</p>
              </div>
            )}
          </div>
          {mode === 'card' && cardPick && <CardRecommendation card={cardPick} cash={cash} />}
          {cash && <CashRow strategy={cash} onBook={() => setBookingStrategy(cash)} />}
          <div className="space-y-3">
            {results.filter((strategy) => strategy.method !== 'cash').slice(0, 8).map((strategy, index) => (
              <ResultRow key={strategy.id} strategy={strategy} index={index + 1} onBook={() => setBookingStrategy(strategy)} />
            ))}
          </div>
          {!cash && <p className="rounded-xl bg-[#fff8e7] p-4 text-sm text-[#7b6a3a]">We could not load the cash baseline for this route yet.</p>}
          <div className="mt-7 rounded-2xl bg-[#f4f6f2] p-4 text-xs leading-5 text-[#7e8780]">
            <Info className="mr-2 inline h-3.5 w-3.5 align-[-2px]" />
            Fares come from Booking.com. A card code is honoured on the site named next to it, not on Booking.com. Points come off the fare at the rupee value published for that card.
          </div>
        </section>
      </div>
      {bookingStrategy && <BookingModal strategy={bookingStrategy} destination={destination} onClose={() => setBookingStrategy(undefined)} />}
    </div>
  );
}

function CashRow({ strategy, onBook }: { strategy: RedemptionStrategy; onBook: () => void }) {
  return (
    <div className="mb-4 rounded-2xl border border-[#dfe4df] bg-[#fbfcfa] p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[10px] font-bold uppercase tracking-wider text-[#8a938c]">Cash baseline</p>
          <p className="mt-1 font-semibold text-[#202522]">{strategy.strategyName}</p>
          <p className="mt-0.5 text-xs text-[#7c867e]">Booking.com</p>
        </div>
        <div className="shrink-0 text-right">
          {strategy.listPrice ? <p className="text-xs text-[#9aa19c] line-through tabular-nums">{inr(strategy.listPrice)}</p> : null}
          <p className="text-xl font-bold tabular-nums text-[#202522]">{inr(strategy.totalOutCost)}</p>
          <button onClick={onBook} className="mt-1 text-xs font-semibold text-[#c26f2e] hover:underline">Book this fare</button>
        </div>
      </div>
      {strategy.offerSite && (
        <div className="mt-3 flex items-center justify-between gap-3 border-t border-[#edf0ec] pt-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-[#202522]">{strategy.offerSite}</p>
            <p className="text-xs text-[#7c867e]">Card offer on this search</p>
          </div>
          <div className="shrink-0 text-right">
            <p className="text-sm font-semibold tabular-nums text-[#202522]">{strategy.offerLabel}</p>
            {strategy.offerTerms && (
              <a href={strategy.offerTerms} target="_blank" rel="noreferrer" className="mt-0.5 inline-block text-xs font-semibold text-[#c26f2e] hover:underline">Check terms</a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function CardRecommendation({ card, cash }: { card: CardPick; cash?: RedemptionStrategy }) {
  return (
    <div className="mb-5 rounded-2xl border border-[#f0d48a] bg-[#fffaf0] p-4">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[#f6bd19] text-[#202522]"><CreditCard className="h-4 w-4" /></div>
        <div className="min-w-0 flex-1">
          <p className="text-[10px] font-bold uppercase tracking-wider text-[#9b7512]">Worth considering</p>
          <p className="mt-1 font-semibold text-[#202522]">{card.name}</p>
          <p className="mt-1 text-xs leading-5 text-[#7e7252]">{card.pitch || `This card could reduce a ${cash ? inr(cash.totalOutCost) : 'cash'} fare through its points or an offer.`}</p>
        </div>
        {card.listingUrl && (
          <a href={card.listingUrl} target="_blank" rel="noreferrer" className="shrink-0 rounded-lg bg-[#202a2c] px-3 py-2 text-xs font-semibold text-white">Compare cards</a>
        )}
        {card.termsUrl && (
          <a href={card.termsUrl} target="_blank" rel="noreferrer" className="shrink-0 self-center text-xs font-semibold text-[#c26f2e] hover:underline">Check terms</a>
        )}
      </div>
    </div>
  );
}

function ResultRow({ strategy, index, onBook }: { strategy: RedemptionStrategy; index: number; onBook: () => void }) {
  const isBest = strategy.badge === 'best';
  return (
    <div className={`rounded-2xl border p-4 transition hover:-translate-y-0.5 hover:shadow-md ${isBest ? 'border-[#a9d1b0] bg-[#f5fbf5]' : 'border-[#e5e9e4] bg-white'}`}>
      <div className="flex flex-col gap-4 md:flex-row md:items-center">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-xs font-bold ${isBest ? 'bg-[#287447] text-white' : 'bg-[#eef1ed] text-[#7c867e]'}`}>{index}</div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-semibold text-[#202522]">{strategy.strategyName}</h3>
              {isBest && <span className="rounded-full bg-[#d8f0dc] px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-[#287447]">Best deal</span>}
            </div>
            <p className="mt-1 text-xs leading-5 text-[#7c867e]">{strategy.description}</p>
            <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-[#89918b]">
              <span className="inline-flex items-center gap-1"><CreditCard className="h-3 w-3" /> {strategy.cardName}</span>
              <span className="inline-flex items-center gap-1"><Plane className="h-3 w-3" /> {strategy.airlineName}</span>
              {strategy.termsUrl && (
                <a href={strategy.termsUrl} target="_blank" rel="noreferrer" className="font-semibold text-[#c26f2e] hover:underline">Check terms</a>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center justify-between gap-5 border-t border-[#edf0ec] pt-3 md:border-l md:border-t-0 md:pl-5 md:pt-0">
          <div className="grid grid-cols-3 gap-4">
            <MiniMetric label="You pay" value={inr(strategy.totalOutCost)} accent={isBest} />
            <MiniMetric label="Points" value={strategy.pointsUsed ? strategy.pointsUsed.toLocaleString('en-IN') : '—'} />
            <MiniMetric label="Value" value={strategy.valuePerPoint ? `₹${strategy.valuePerPoint.toFixed(2)}` : 'Cash'} />
          </div>
          <button onClick={onBook} className="flex h-10 shrink-0 items-center gap-1.5 rounded-xl bg-[#202a2c] px-3 text-xs font-semibold text-white transition hover:bg-[#334447]">
            Book <ExternalLink className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

function BookingModal({ strategy, destination, onClose }: { strategy: RedemptionStrategy; destination?: Destination; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#13201d]/70 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-[26px] bg-[#f7f7f2] p-5 shadow-2xl md:p-7">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-[.18em] text-[#7d867f]">Ready to book</p>
            <h2 className="mt-2 text-2xl font-semibold text-[#202522]">{destination?.city} with {strategy.airlineName}</h2>
          </div>
          <button onClick={onClose} className="rounded-full p-2 text-[#778079] hover:bg-black/5"><X className="h-5 w-5" /></button>
        </div>
        <div className="mt-5 rounded-2xl bg-white p-4">
          <p className="text-sm font-semibold text-[#202522]">{bookingTitle(strategy)}</p>
          <p className="mt-2 text-xs leading-5 text-[#7e8780]">
            {strategy.method === 'cash'
              ? `This opens the Booking.com search for this flight. Estimated total ${inr(strategy.totalOutCost)}.`
              : `${strategy.description} The Booking.com fare is ${inr(strategy.totalOutCost)} after the points value we can count.`}
          </p>
        </div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <button onClick={onClose} className="h-12 rounded-xl border border-[#dfe4df] text-sm font-semibold text-[#5d6861]">Keep comparing</button>
          {strategy.bookingUrl ? (
            <a href={strategy.bookingUrl} target="_blank" rel="noreferrer" className="flex h-12 items-center justify-center gap-2 rounded-xl bg-[#202a2c] text-sm font-semibold text-white">
              Continue to booking <ExternalLink className="h-4 w-4" />
            </a>
          ) : (
            <button disabled className="h-12 rounded-xl bg-[#202a2c] text-sm font-semibold text-white opacity-40">Continue to booking</button>
          )}
        </div>
        <p className="mt-4 text-center text-[11px] text-[#9aa19c]">Fares are booked on Booking.com. Card codes belong to the site named on the offer.</p>
      </div>
    </div>
  );
}

function MiniMetric({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="min-w-[84px]">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-[#9aa19c]">{label}</p>
      <p className={`mt-1 text-sm font-bold tabular-nums ${accent ? 'text-[#287447]' : 'text-[#202522]'}`}>{value}</p>
    </div>
  );
}

const MONTHS: Record<string, number> = {
  jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4,
  may: 5, jun: 6, june: 6, jul: 7, july: 7, aug: 8, august: 8,
  sep: 9, sept: 9, september: 9, oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12,
};

const PLACE_ALIASES: Record<string, string> = {
  delhi: 'delhi', 'new delhi': 'delhi', bangalore: 'bangalore', bengaluru: 'bangalore', bombay: 'mumbai',
};

function isoDate(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function findPlace(raw: string) {
  const text = raw.trim().toLowerCase();
  if (!text) return undefined;
  const words = text.split(/\s+/).filter(Boolean);
  for (const word of [text, ...words]) {
    const alias = PLACE_ALIASES[word];
    if (alias) return destinations.find((destination) => destination.id === alias);
    const byCode = destinations.find((destination) => destination.airportCode.toLowerCase() === word);
    if (byCode) return byCode;
    const byCity = destinations.find((destination) => destination.city.toLowerCase() === word);
    if (byCity) return byCity;
  }
  return destinations.find((destination) => text.includes(destination.city.toLowerCase()));
}

function cityBit(raw: string) {
  return raw
    .replace(/\b(?:on\s+)?\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}(?:\s+\d{4})?\b/gi, '')
    .replace(/\b(?:day after tomorrow|tomorrow|today|tonight|tmrw|tmr)\b/gi, '')
    .replace(/^(?:fly\s+)?from\s+/i, '')
    .split(/[,.]/)[0]
    .trim();
}

function findDate(text: string) {
  const low = text.toLowerCase();
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  if (/\bday after tomorrow\b/.test(low)) {
    today.setDate(today.getDate() + 2);
    return isoDate(today);
  }
  if (/\btomorrow\b|\btmrw?\b/.test(low)) {
    today.setDate(today.getDate() + 1);
    return isoDate(today);
  }
  if (/\btoday\b|\btonight\b/.test(low)) return isoDate(today);
  const match = text.match(/(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})(?:\s+(\d{4}))?/);
  if (!match) return '';
  const day = Number(match[1]);
  const month = MONTHS[match[2].toLowerCase()];
  if (!month || day < 1 || day > 31) return '';
  let year = match[3] ? Number(match[3]) : today.getFullYear();
  const picked = new Date(year, month - 1, day);
  if (!match[3] && picked < new Date(today.getFullYear(), today.getMonth(), today.getDate())) year += 1;
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

function daysFromNow(days: number) {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function parseTrip(raw: string) {
  const text = raw.trim();
  const parts = text.split(/\s+to\s+/i);
  if (parts.length < 2) return null;
  const origin = findPlace(cityBit(parts[0]));
  const destination = findPlace(cityBit(parts[1]));
  if (!origin || !destination || origin.id === destination.id) return null;
  return {
    origin,
    destination,
    depart: findDate(text) || daysFromNow(30),
    cabin: (/business/i.test(text) ? 'business' : 'economy') as Cabin,
  };
}

function cardOption(card: WalletCard) {
  const rate = card.inrPerPoint;
  if (rate != null && rate > 0) {
    const shown = rate.toLocaleString('en-IN', { minimumFractionDigits: rate < 1 ? 2 : 0, maximumFractionDigits: 2 });
    return `${card.name} · ${card.currency} · ₹${shown}`;
  }
  return card.currency ? `${card.name} · ${card.currency}` : card.name;
}

function bookingTitle(strategy: RedemptionStrategy) {
  if (strategy.method === 'transfer') return `First transfer ${strategy.pointsUsed.toLocaleString('en-IN')} points`;
  if (strategy.method === 'points') {
    const name = strategy.pointProgram || 'points';
    return `${strategy.pointsUsed.toLocaleString('en-IN')} ${name} come off this fare`;
  }
  return 'Book this cash fare';
}

function inr(value: number) {
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(value || 0);
}

function formatMonth(value: string) {
  if (!value) return 'Flexible dates';
  const [year, month] = value.split('-');
  return new Date(Number(year), Number(month) - 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
}
