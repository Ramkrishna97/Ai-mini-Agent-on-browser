def is_prime(n):
    """
    Checks if a number is prime.

    Args:
        n: The number to check.

    Returns:
        True if the number is prime, False otherwise.
    """
    if n <= 1:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

def find_first_n_primes(n):
    """
    Finds the first n prime numbers.

    Args:
        n: The number of prime numbers to find.

    Returns:
        A list containing the first n prime numbers.
    """
    primes = []
    num = 2
    while len(primes) < n:
        if is_prime(num):
            primes.append(num)
        num += 1
    return primes

if __name__ == '__main__':
    n = 6
    prime_numbers = find_first_n_primes(n)
    print(f"The first {n} prime numbers are: {prime_numbers}")
