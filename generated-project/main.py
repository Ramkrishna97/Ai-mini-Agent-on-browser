# main.py
import prime_number_finder
import average_calculator
import config

def main():
    """
    Main application entry point. Handles user interaction, calls prime number and average calculation functions, and prints the result.
    """
    try:
        num_primes = config.DEFAULT_NUM_PRIMES
    except NameError:
        num_primes = 6  # Default value if config not available

    primes = prime_number_finder.find_primes(num_primes)
    average = average_calculator.calculate_average(primes)

    print(f"The average of the first {num_primes} odd prime numbers is: {average}")

if __name__ == "__main__":
    main()
